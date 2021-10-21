import torch
import torch.nn as nn
from torch import optim
from torch.utils.tensorboard import SummaryWriter

import numpy as np

import sys
import os
import datetime
from copy import deepcopy
import types

import evals
from utils import build_path, get_label, get_feat
from model import VAE, compute_loss
from fairmodel import FairCritic, compute_fair_loss
from data import load_data

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
sys.path.append('./')
THRESHOLDS = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.15, 0.20, 0.25, 0.30,
              0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.8, 0.85, 0.9, 0.95]

METRICS = ['ACC', 'HA', 'ebF1', 'miF1', 'maF1', 'meanAUC', 'medianAUC', 'meanAUPR', 'medianAUPR',
           'meanFDR', 'medianFDR', 'p_at_1', 'p_at_3', 'p_at_5']


def train_mpvae_one_epoch(data, model, optimizer, scheduler, args, eval_after_one_epoch=True):
    np.random.shuffle(data.train_idx)

    if eval_after_one_epoch:
        smooth_nll_loss=0.0 # label encoder decoder cross entropy loss
        smooth_nll_loss_x=0.0 # feature encoder decoder cross entropy loss
        smooth_c_loss = 0.0 # label encoder decoder ranking loss
        smooth_c_loss_x=0.0 # feature encoder decoder ranking loss
        smooth_kl_loss = 0.0 # kl divergence
        smooth_total_loss=0.0 # total loss
        smooth_macro_f1 = 0.0 # macro_f1 score
        smooth_micro_f1 = 0.0 # micro_f1 score
        #smooth_l2_loss = 0.0

        temp_label = []
        temp_indiv_prob = []

    for i in range(int(len(data.train_idx) / float(data.batch_size)) + 1):
        optimizer.zero_grad()
        start = i * data.batch_size
        end = min(data.batch_size * (i + 1), len(data.train_idx))

        input_feat = data.input_feat[data.train_idx[start:end]]
        input_feat = torch.from_numpy(input_feat).to(device)

        input_label = data.labels[data.train_idx[start:end]]
        input_label = torch.from_numpy(input_label)
        input_label = deepcopy(input_label).float().to(device)

        label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar = model(
            input_label, input_feat)

        if args.residue_sigma == "random":
            r_sqrt_sigma = torch.from_numpy(
                np.random.uniform(
                    -np.sqrt(6.0 / (args.label_dim + args.z_dim)),
                    np.sqrt(6.0 / (args.label_dim + args.z_dim)), (args.label_dim, args.z_dim))).to(
                device)
            total_loss, nll_loss, nll_loss_x, c_loss, c_loss_x, kl_loss, indiv_prob = compute_loss(
                input_label, label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar,
                r_sqrt_sigma, args)
        else:
            total_loss, nll_loss, nll_loss_x, c_loss, c_loss_x, kl_loss, indiv_prob = compute_loss(
                input_label, label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar,
                model.r_sqrt_sigma, args)

        total_loss.backward()
        grad_norm = nn.utils.clip_grad_norm_(model.parameters(), 100)
        optimizer.step()
        if scheduler:
            scheduler.step()

        if eval_after_one_epoch:
            # evaluation
            train_metrics = evals.compute_metrics(
                indiv_prob.cpu().data.numpy(), input_label.cpu().data.numpy(), 0.5,
                all_metrics=False)
            macro_f1, micro_f1 = train_metrics['maF1'], train_metrics['miF1']

            smooth_nll_loss += nll_loss
            smooth_nll_loss_x += nll_loss_x
            # smooth_l2_loss += l2_loss
            smooth_c_loss += c_loss
            smooth_c_loss_x += c_loss_x
            smooth_kl_loss += kl_loss
            smooth_total_loss += total_loss
            smooth_macro_f1 += macro_f1
            smooth_micro_f1 += micro_f1

            temp_label.append(input_label.cpu().data.numpy())  # log the labels
            temp_indiv_prob.append(
                indiv_prob.detach().data.cpu().numpy())  # log the individual prediction of the probability on each label

    if eval_after_one_epoch:

        nll_loss = smooth_nll_loss / float(i)
        nll_loss_x = smooth_nll_loss_x / float(i)
        c_loss = smooth_c_loss / float(i)
        c_loss_x = smooth_c_loss_x / float(i)
        kl_loss = smooth_kl_loss / float(i)
        total_loss = smooth_total_loss / float(i)
        macro_f1 = smooth_macro_f1 / float(i)
        micro_f1 = smooth_micro_f1 / float(i)

        temp_indiv_prob = np.reshape(np.array(temp_indiv_prob), (-1))
        temp_label = np.reshape(np.array(temp_label), (-1))

        time_str = datetime.datetime.now().isoformat()
        print(
            "%s\nmacro_f1=%.6f, micro_f1=%.6f\nnll_loss=%.6f\tnll_loss_x=%.6f\nc_loss=%.6f\tc_loss_x=%.6f\tkl_loss=%.6f\ntotal_loss=%.6f\n" % (
            time_str, macro_f1, micro_f1, nll_loss * args.nll_coeff,
            nll_loss_x * args.nll_coeff, c_loss * args.c_coeff, c_loss_x * args.c_coeff, kl_loss,
            total_loss))

        current_loss, val_metrics = validate_mpvae(
            model, data.input_feat, data.labels, data.valid_idx, args)


def hard_cluster(model, data, args, use_valid=True):
    # todo: do we have soft clustering algorithm? For example, can we use gumble softmax?

    with torch.no_grad():
        model.eval()
        if use_valid:
            idxs = data.valid_idx
        else:
            idxs = data.train_idx
        idxs.sort()

        labels_mu, labels_logvar = [], []
        for i in range(int(len(idxs) / float(data.batch_size)) + 1):
            start = i * data.batch_size
            end = min(data.batch_size * (i + 1), len(idxs))

            input_feat = data.input_feat[idxs[start:end]]
            input_feat = torch.from_numpy(input_feat).to(device)

            input_label = data.labels[idxs[start:end]]
            input_label = torch.from_numpy(input_label).to(device)
            label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar = model(
                input_label, input_feat)
            labels_mu.append(label_mu.cpu().data.numpy())
            labels_logvar.append(label_logvar.cpu().data.numpy())

        labels_mu = np.concatenate(labels_mu)
        labels_logvar = np.concatenate(labels_logvar)

        # todo: how to properly cluster labels based on JSD or KL average distance?
        #  Take KL for instance, afer merging two points, the new cluster is a Gaussian mixture,
        #  do we still have closed form formula to update new distance?
        from sklearn.cluster import AgglomerativeClustering
        distance_threshold = args.labels_cluster_distance_threshold
        succ_cluster = False
        for cluster_try in range(10):
            cluster = AgglomerativeClustering(
                n_clusters=None, distance_threshold=args.labels_cluster_distance_threshold).fit(
                labels_mu)
            labels_cluster = cluster.labels_
            _, counts = np.unique(labels_cluster, return_counts=True)
            if counts.min() < args.labels_cluster_min_size:
                distance_threshold *= 2
            else:
                succ_cluster = True
                break

        if succ_cluster is False:
            raise UserWarning('Labels clustering not converged')

        assert labels_cluster.shape.shape[1] == labels_mu.shape[1]
        return labels_cluster


def regularzie_mpvae_unfair(data, model, optimizer, use_valid=True):
    if use_valid:
        idxs = data.valid_idx
    else:
        idxs = data.train_idx

    np.random.shuffle(idxs)

    optimizer.zero_grad()

    labels_z, feats_z = [], []
    for i in range(int(len(idxs) / float(data.batch_size)) + 1):
        start = i * data.batch_size
        end = min(data.batch_size * (i + 1), len(idxs))

        input_feat = data.input_feat[idxs[start:end]]
        input_feat = torch.from_numpy(input_feat).to(device)

        input_label = data.labels[idxs[start:end]]
        input_label = torch.from_numpy(input_label).to(device)
        label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar = model(
            input_label, input_feat)

        label_z = model.label_reparameterize(label_mu, label_logvar)
        feat_z = model.feat_reparameterize(feat_mu, feat_logvar)

        labels_z.append(labels_z)
        feats_z.append(feat_z)
    labels_z = torch.cat(labels_z)
    feats_z = torch.cat(feats_z)

    clusters = data.label_clusters[idxs]
    sensitive_feat = data.sensitive_feat[idxs]

    labels_z_unfair = 0.
    feats_z_unfair = 0.
    label_centroids = np.unique(clusters)
    sensitive_centroids = np.unique(sensitive_feat, axis=0)
    for centroid in label_centroids:
        cluster_labels_z = labels_z[clusters == centroid]
        if cluster_labels_z:
            for sensitive in sensitive_centroids:
                sensitive_centroid = torch.all([
                    torch.all(torch.equal(sensitive_centroids, sensitive), axis=1),  # sensitive level
                    clusters == centroid], axis=1)
                cluster_labels_z_sensitive = labels_z[sensitive_centroid]
                if cluster_labels_z_sensitive:
                    labels_z_unfair += torch.pow(
                        cluster_labels_z_sensitive.mean() - cluster_labels_z.mean(), 2)

        cluster_feats_z = feats_z[clusters == centroid]
        if cluster_feats_z:
            for sensitive in sensitive_centroids:
                sensitive_centroid = torch.all([
                    torch.all(torch.equal(sensitive_centroids, sensitive), axis=1),  # sensitive level
                    clusters == centroid], axis=1)
                cluster_feats_z_sensitive = feats_z[sensitive_centroid]
                if cluster_feats_z_sensitive:
                    feats_z_unfair += torch.pow(
                        cluster_feats_z_sensitive.mean() - cluster_feats_z.mean(), 2)

    fairloss = labels_z_unfair + feats_z_unfair
    fairloss.backward()
    optimizer.step()


def train_fair_through_regularize(args):
    param_setting = "lr-{}_lr-decay_{:.2f}_lr-times_{:.1f}_nll-{:.2f}_l2-{:.2f}_c-{:.2f}".format(
        args.learning_rate, args.lr_decay_ratio, args.lr_decay_times, args.nll_coeff, args.l2_coeff,
        args.c_coeff)

    build_path('fairreg/summary/{}/{}'.format(args.dataset, param_setting))
    build_path('fairreg/model/model_{}/{}'.format(args.dataset, param_setting))
    summary_dir = 'fairreg/summary/{}/{}'.format(args.dataset, param_setting)
    model_dir = 'fairreg/model/model_{}/{}'.format(args.dataset, param_setting)
    writer = SummaryWriter(log_dir=summary_dir)

    # train a prior mpvae
    np.random.seed(4)
    input_feat, labels = load_data(args.dataset, args.mode)
    train_cnt, valid_cnt = int(len(input_feat) * 0.7), int(len(input_feat) * .2)
    train_idx = np.arange(train_cnt)
    valid_idx = np.arange(train_cnt, valid_cnt + train_cnt)

    one_epoch_iter = np.ceil(len(train_idx) / args.batch_size)
    n_iter = one_epoch_iter * args.max_epoch

    data = types.SimpleNamespace(
        input_feat=input_feat, labels=labels,
        train_idx=train_idx, valid_idx=valid_idx,
        batch_size=args.batch_size)
    args.feature_dim = input_feat.shape[1]
    args.label_dim = labels.shape[1]

    prior_vae = VAE(args).to(device)
    prior_vae.train()

    optimizer = optim.Adam(prior_vae.parameters(), lr=args.learning_rate, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.StepLR(
        optimizer, one_epoch_iter * (args.max_epoch / args.lr_decay_times), args.lr_decay_ratio)

    if args.resume and os.path.exists(args.checkpoint_path):
        # todo: load from pretrained
        pass
    #     prior_vae.load_state_dict(torch.load(args.checkpoint_path))
    #     current_step = int(args.checkpoint_path.split('/')[-1].split('-')[-1])
    #     print("loaded model: %s" % args.label_checkpoint_path)
    # else:
    #     current_step = 0
    current_step = 0

    smooth_nll_loss=0.0 # label encoder decoder cross entropy loss
    smooth_nll_loss_x=0.0 # feature encoder decoder cross entropy loss
    smooth_c_loss = 0.0 # label encoder decoder ranking loss
    smooth_c_loss_x=0.0 # feature encoder decoder ranking loss
    smooth_kl_loss = 0.0 # kl divergence
    smooth_total_loss=0.0 # total loss
    smooth_macro_f1 = 0.0 # macro_f1 score
    smooth_micro_f1 = 0.0 # micro_f1 score
    #smooth_l2_loss = 0.0

    best_loss = 1e10
    best_iter = 0
    best_macro_f1 = 0.0 # best macro f1 for ckpt selection in validation
    best_micro_f1 = 0.0 # best micro f1 for ckpt selection in validation
    best_acc = 0.0 # best subset acc for ckpt selction in validation

    temp_label=[]
    temp_indiv_prob=[]

    best_test_metrics = None

    for _ in range(args.max_epoch // 5):
        train_mpvae_one_epoch(data, prior_vae, optimizer, scheduler, args)
    label_clusters = hard_cluster(prior_vae, data)

    # retrain a new mpvae + fair regularization
    np.random.seed(4)
    nonsensitive_feat, sensitive_feat, labels = load_data(args.dataset, args.mode, True)
    train_cnt, valid_cnt = int(len(nonsensitive_feat) * 0.7), int(len(nonsensitive_feat) * .2)
    train_idx = np.arange(train_cnt)
    valid_idx = np.arange(train_cnt, valid_cnt + train_cnt)

    data = types.SimpleNamespace(
        input_feat=nonsensitive_feat, labels=labels, train_idx=train_idx, valid_idx=valid_idx,
        batch_size=args.batch_size, label_clusters=label_clusters, sensitive_feat=sensitive_feat)
    args.feature_dim = nonsensitive_feat.shape[1]
    args.label_dim = labels.shape[1]

    fair_vae = VAE(args).to(device)
    fair_vae.train()

    optimizer = optim.Adam(fair_vae.parameters(), lr=args.learning_rate, weight_decay=1e-5)
    optimizer_fair = optim.Adam(fair_vae.parameters(), lr=args.learning_rate, weight_decay=1e-5)
    scheduler = torch.optim.lr_scheduler.StepLR(
        optimizer, one_epoch_iter * (args.max_epoch / args.lr_decay_times), args.lr_decay_ratio)

    for _ in range(args.max_epoch):
        train_mpvae_one_epoch(data, fair_vae, optimizer, scheduler, args)
        regularzie_mpvae_unfair(data, fair_vae, optimizer_fair, use_valid=True)


def validate_mpvae(model, feat, labels, valid_idx, args):
    with torch.no_grad():
        model.eval()
        print("performing validation...")

        all_nll_loss = 0
        all_l2_loss = 0
        all_c_loss = 0
        all_total_loss = 0

        all_indiv_prob = []
        all_label = []

        real_batch_size = min(args.batch_size, len(valid_idx))
        for i in range(int((len(valid_idx) - 1) / real_batch_size) + 1):
            start = real_batch_size * i
            end = min(real_batch_size * (i + 1), len(valid_idx))
            input_feat = feat[valid_idx[start:end]]
            input_label = labels[valid_idx[start:end]]
            input_feat, input_label = torch.from_numpy(input_feat).to(device), torch.from_numpy(
                input_label)
            input_label = deepcopy(input_label).float().to(device)

            label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar = model(
                input_label, input_feat)
            total_loss, nll_loss, nll_loss_x, c_loss, c_loss_x, kl_loss, indiv_prob = compute_loss(
                input_label, label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar,
                model.r_sqrt_sigma, args)

            all_nll_loss += nll_loss * (end - start)
            # all_l2_loss += l2_loss*(end-start)
            all_c_loss += c_loss * (end - start)
            all_total_loss += total_loss * (end - start)

            for j in deepcopy(indiv_prob).cpu().data.numpy():
                all_indiv_prob.append(j)
            for j in deepcopy(input_label).cpu().data.numpy():
                all_label.append(j)

        # collect all predictions and ground-truths
        all_indiv_prob = np.array(all_indiv_prob)
        all_label = np.array(all_label)

        nll_loss = all_nll_loss / len(valid_idx)
        l2_loss = all_l2_loss / len(valid_idx)
        c_loss = all_c_loss / len(valid_idx)
        total_loss = all_total_loss / len(valid_idx)

        best_val_metrics = None
        for threshold in THRESHOLDS:
            val_metrics = evals.compute_metrics(all_indiv_prob, all_label, threshold, all_metrics=True)

            if best_val_metrics == None:
                best_val_metrics = {}
                for metric in METRICS:
                    best_val_metrics[metric] = val_metrics[metric]
            else:
                for metric in METRICS:
                    if 'FDR' in metric:
                        best_val_metrics[metric] = min(best_val_metrics[metric], val_metrics[metric])
                    else:
                        best_val_metrics[metric] = max(best_val_metrics[metric], val_metrics[metric])

        time_str = datetime.datetime.now().isoformat()
        acc, ha, ebf1, maf1, mif1 = best_val_metrics['ACC'], best_val_metrics['HA'], best_val_metrics[
            'ebF1'], best_val_metrics['maF1'], best_val_metrics['miF1']

        # nll_coeff: BCE coeff, lambda_1
        # c_coeff: Ranking loss coeff, lambda_2
        print("**********************************************")
        print(
            "valid results: %s\nacc=%.6f\tha=%.6f\texam_f1=%.6f, macro_f1=%.6f, micro_f1=%.6f\nnll_loss=%.6f\tc_loss=%.6f\ttotal_loss=%.6f" % (
                time_str, acc, ha, ebf1, maf1, mif1, nll_loss * args.nll_coeff, c_loss * args.c_coeff,
                total_loss))
        print("**********************************************")

    model.train()

    return nll_loss, best_val_metrics



if __name__ == '__main__':
    import argparse
    from main import parser

    parser.add_argument('-labels_cluster_distance_threshold', type=float, default=.1)
    parser.add_argument('-labels_cluster_min_size', type=float, default=4)

    args = parser.parse_args()

    train_fair_through_regularize(args)