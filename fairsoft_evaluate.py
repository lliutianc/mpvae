from copy import deepcopy
import os
import types
import pickle

import torch
import torch.nn as nn

import numpy as np
from tqdm import tqdm

import evals
from model import VAE, compute_loss
from data import load_data
from faircluster_train import THRESHOLDS, METRICS
from faircluster_train import parser
from label_cluster import construct_label_clusters


IMPLEMENTED_METHODS = ['arule', 'indication_function']


def evaluate_mpvae(model, data, target_fair_labels, label_distances, eval_fairness=True, eval_train=True, eval_valid=True):
    if eval_fairness and target_fair_labels is None:
        target_fair_labels = list(label_distances.keys())
        raise NotImplementedError('Have not supported smooth-OD yet.')

    target_fair_labels_str = []
    for target_fair_label in target_fair_labels:
        target_fair_label = ''.join(target_fair_label.astype(str))
        target_fair_labels_str.append(target_fair_label)
    target_fair_labels = target_fair_labels_str


    with torch.no_grad():
        model.eval()

        if eval_train:
            train_nll_loss = 0
            train_c_loss = 0
            train_total_loss = 0

            train_indiv_prob = []
            train_label = []

            if eval_fairness:
                train_feat_z = []
                train_sensitive = data.sensitive_feat[data.train_idx]
            with tqdm(
                    range(int(len(data.train_idx) / float(data.batch_size)) + 1),
                    desc='Evaluate on training set') as t:

                for i in t:
                    start = i * data.batch_size
                    end = min(data.batch_size * (i + 1), len(data.train_idx))
                    idx = data.train_idx[start:end]

                    input_feat = torch.from_numpy(
                        data.input_feat[idx]).to(args.device)

                    input_label = torch.from_numpy(data.labels[idx])
                    input_label = deepcopy(input_label).float().to(args.device)

                    label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar = model(
                        input_label, input_feat)
                    total_loss, nll_loss, nll_loss_x, c_loss, c_loss_x, kl_loss, indiv_prob = compute_loss(
                        input_label, label_out, label_mu, label_logvar, feat_out, feat_mu,
                        feat_logvar, model.r_sqrt_sigma, args)

                    train_nll_loss += nll_loss.item() * (end - start)
                    train_c_loss += c_loss.item() * (end - start)
                    train_total_loss += total_loss.item() * (end - start)

                    for j in deepcopy(indiv_prob).cpu().data.numpy():
                        train_indiv_prob.append(j)
                    for j in deepcopy(input_label).cpu().data.numpy():
                        train_label.append(j)

                    if eval_fairness:
                        feat_z = model.feat_reparameterize(
                            feat_mu, feat_logvar)
                        train_feat_z.append(feat_z.cpu().data.numpy())

                train_indiv_prob = np.array(train_indiv_prob)
                train_label = np.array(train_label)

                nll_loss = train_nll_loss / len(data.train_idx)
                c_loss = train_c_loss / len(data.train_idx)
                total_loss = train_total_loss / len(data.train_idx)

                best_val_metrics = None
                for threshold in THRESHOLDS:
                    val_metrics = evals.compute_metrics(
                        train_indiv_prob, train_label, threshold, all_metrics=True)

                    if best_val_metrics is None:
                        best_val_metrics = {}
                        for metric in METRICS:
                            best_val_metrics[metric] = val_metrics[metric]
                    else:
                        for metric in METRICS:
                            if 'FDR' in metric:
                                best_val_metrics[metric] = min(
                                    best_val_metrics[metric], val_metrics[metric])
                            else:
                                best_val_metrics[metric] = max(
                                    best_val_metrics[metric], val_metrics[metric])

                acc, ha, ebf1, maf1, mif1 = best_val_metrics['ACC'], best_val_metrics['HA'], \
                    best_val_metrics['ebF1'], best_val_metrics['maF1'], \
                    best_val_metrics['miF1']

                if eval_fairness and label_dist is not None:
                    train_feat_z = np.concatenate(train_feat_z)
                    assert train_feat_z.shape[0] == len(data.train_idx) and \
                        train_feat_z.shape[1] == args.latent_dim

                    sensitive_feat = np.unique(train_sensitive, axis=0)
                    idxs = np.arange(len(data.train_idx))

                    mean_diffs = []
                    for target_fair_label in target_fair_labels:
                        target_label_dist = label_distances[target_fair_label]
                        weights = []
                        for label in data.labels[idxs]:
                            label = label.astype(int)
                            distance = target_label_dist.get(
                                ''.join(label.astype(str)), np.inf)
                            weights.append(distance)
                        weights = np.array(weights).reshape(-1, 1)

                        if weights.sum() > 0:
                            feat_z_weighted = np.sum(
                                feat_z * weights, axis=0) / weights.sum()

                            sensitive_centroid = np.unique(
                                train_sensitive, axis=0)
                            for sensitive in sensitive_centroid:
                                target_sensitive = np.all(
                                    np.equal(train_sensitive, sensitive), axis=1)
                                feat_z_sensitive = feat_z[idxs[target_sensitive]]
                                weights_sensitive = weights[idxs[target_sensitive]]
                                if weights_sensitive.sum() > 0:
                                    unfair_feat_z_sen = np.sum(
                                        feat_z_sensitive * weights_sensitive, 0) / weights_sensitive.sum()
                                    mean_diffs.append(
                                        np.mean(np.power(unfair_feat_z_sen - feat_z_weighted, 2)))

                    mean_diffs = np.mean(mean_diffs)

                    # nll_coeff: BCE coeff, lambda_1
                    # c_coeff: Ranking loss coeff, lambda_2
                    print("********************train********************")
                    print(
                        ' & '.join([
                            str(round(m, 4)) for m in [
                                acc, ha, ebf1, maf1, mif1, mean_diffs]]))
                else:
                    # nll_coeff: BCE coeff, lambda_1
                    # c_coeff: Ranking loss coeff, lambda_2
                    print("********************train********************")
                    print(
                        ' & '.join(
                            [str(round(m, 4)) for m in [acc, ha, ebf1, maf1, mif1]]))

            train_best_metrics = best_val_metrics
        else:
            train_best_metrics = None

        if eval_valid:
            valid_nll_loss = 0
            valid_c_loss = 0
            valid_total_loss = 0

            valid_indiv_prob = []
            valid_label = []

            if eval_fairness:
                valid_feat_z = []
                valid_sensitive = data.sensitive_feat[data.valid_idx]
            with tqdm(
                    range(int(len(data.valid_idx) / float(data.batch_size)) + 1),
                    desc='Evaluate on validation set') as t:

                for i in t:
                    start = i * data.batch_size
                    end = min(data.batch_size * (i + 1), len(data.valid_idx))
                    idx = data.valid_idx[start:end]

                    input_feat = torch.from_numpy(
                        data.input_feat[idx]).to(args.device)

                    input_label = torch.from_numpy(data.labels[idx])
                    input_label = deepcopy(input_label).float().to(args.device)

                    label_out, label_mu, label_logvar, feat_out, feat_mu, feat_logvar = model(
                        input_label, input_feat)
                    total_loss, nll_loss, nll_loss_x, c_loss, c_loss_x, kl_loss, indiv_prob = compute_loss(
                        input_label, label_out, label_mu, label_logvar, feat_out, feat_mu,
                        feat_logvar, model.r_sqrt_sigma, args)

                    valid_nll_loss += nll_loss.item() * (end - start)
                    valid_c_loss += c_loss.item() * (end - start)
                    valid_total_loss += total_loss.item() * (end - start)

                    for j in deepcopy(indiv_prob).cpu().data.numpy():
                        valid_indiv_prob.append(j)
                    for j in deepcopy(input_label).cpu().data.numpy():
                        valid_label.append(j)

                    if eval_fairness:
                        feat_z = model.feat_reparameterize(
                            feat_mu, feat_logvar)
                        valid_feat_z.append(feat_z.cpu().data.numpy())

                valid_indiv_prob = np.array(valid_indiv_prob)
                valid_label = np.array(valid_label)

                nll_loss = valid_nll_loss / len(data.valid_idx)
                c_loss = valid_c_loss / len(data.valid_idx)
                total_loss = valid_total_loss / len(data.valid_idx)

                best_val_metrics = None
                for threshold in THRESHOLDS:
                    val_metrics = evals.compute_metrics(
                        valid_indiv_prob, valid_label, threshold, all_metrics=True)

                    if best_val_metrics is None:
                        best_val_metrics = {}
                        for metric in METRICS:
                            best_val_metrics[metric] = val_metrics[metric]
                    else:
                        for metric in METRICS:
                            if 'FDR' in metric:
                                best_val_metrics[metric] = min(
                                    best_val_metrics[metric], val_metrics[metric])
                            else:
                                best_val_metrics[metric] = max(
                                    best_val_metrics[metric], val_metrics[metric])

                acc, ha, ebf1, maf1, mif1 = best_val_metrics['ACC'], best_val_metrics['HA'], \
                    best_val_metrics['ebF1'], best_val_metrics['maF1'], \
                    best_val_metrics['miF1']

                if eval_fairness and label_dist is not None:
                    valid_feat_z = np.concatenate(valid_feat_z)
                    assert valid_feat_z.shape[0] == len(data.valid_idx) and \
                        valid_feat_z.shape[1] == args.latent_dim

                    sensitive_feat = np.unique(valid_sensitive, axis=0)
                    idxs = np.arange(len(data.valid_idx))

                    mean_diffs = []
                    for target_fair_label in target_fair_labels:
                        target_label_dist = label_distances[target_fair_label]
                        weights = []
                        for label in data.labels[idxs]:
                            label = label.astype(int)
                            distance = target_label_dist.get(
                                ''.join(label.astype(str)), np.inf)
                            weights.append(distance)
                        weights = np.array(weights).reshape(-1, 1)

                        if weights.sum() > 0:
                            feat_z_weighted = np.sum(
                                feat_z * weights, axis=0) / weights.sum()

                            sensitive_centroid = np.unique(
                                valid_sensitive, axis=0)
                            for sensitive in sensitive_centroid:
                                target_sensitive = np.all(
                                    np.equal(valid_sensitive, sensitive), axis=1)
                                feat_z_sensitive = feat_z[idxs[target_sensitive]]
                                weights_sensitive = weights[idxs[target_sensitive]]
                                if weights_sensitive.sum() > 0:
                                    unfair_feat_z_sen = np.sum(
                                        feat_z_sensitive * weights_sensitive, 0) / weights_sensitive.sum()
                                    mean_diffs.append(
                                        np.mean(np.power(unfair_feat_z_sen - feat_z_weighted, 2)))

                    mean_diffs = np.mean(mean_diffs)

                    # nll_coeff: BCE coeff, lambda_1
                    # c_coeff: Ranking loss coeff, lambda_2
                    print("********************valid********************")
                    print(
                        ' & '.join(
                            [str(round(m, 4)) for m in [acc, ha, ebf1, maf1, mif1, mean_diffs]]))
                else:
                    # nll_coeff: BCE coeff, lambda_1
                    # c_coeff: Ranking loss coeff, lambda_2
                    print("********************valid********************")
                    print(
                        ' & '.join(
                            [str(round(m, 4)) for m in [acc, ha, ebf1, maf1, mif1]]))

            valid_best_metrics = best_val_metrics
        else:
            valid_best_metrics = None

    return train_best_metrics, valid_best_metrics


def search_files(path, filetype):
    files = []
    for file in os.listdir(path):
        if file[-len(filetype):] == filetype:
            files.append(file)
    return files


if __name__ == '__main__':

    args = parser.parse_args()
    args.device = torch.device(
        f"cuda:{args.cuda}" if torch.cuda.is_available() else "cpu")
    args.model_dir = f'fair_through_distance/model/{args.dataset}'

    np.random.seed(4)
    nonsensitive_feat, sensitive_feat, labels = load_data(
        args.dataset, args.mode, True)
    train_cnt, valid_cnt = int(
        len(nonsensitive_feat) * 0.7), int(len(nonsensitive_feat) * .2)
    train_idx = np.arange(train_cnt)
    valid_idx = np.arange(train_cnt, valid_cnt + train_cnt)
    data = types.SimpleNamespace(
        input_feat=nonsensitive_feat, labels=labels, train_idx=train_idx,
        valid_idx=valid_idx, batch_size=args.batch_size, label_clusters=None,
        sensitive_feat=sensitive_feat)
    args.feature_dim = data.input_feat.shape[1]
    args.label_dim = data.labels.shape[1]

    # Test fairness on some labels
    label_type, count = np.unique(labels, axis=0, return_counts=True)
    count_sort_idx = np.argsort(-count)
    label_type = label_type[count_sort_idx]
    target_fair_labels = label_type[:1].astype(int)

    for label_dist_metric in IMPLEMENTED_METHODS:
        label_dist_files = search_files(
            os.path.join(args.model_dir, label_dist_metric), '.npy')
        if len(label_dist_files):
            print(f'Evaluate fairness definition: {label_dist_metric}...')
            label_dist_file = label_dist_files[0]
            label_dist = pickle.load(open(os.path.join(
                args.model_dir, label_dist_metric, label_dist_file), 'rb'))

            for model_prior in IMPLEMENTED_METHODS:
                model_files = search_files(os.path.join(
                    args.model_dir, model_prior), '.pkl')
                if len(model_files):
                    model_file = model_files[0]
                    print(f'try loading model from: {model_file}')

                    model = VAE(args).to(args.device)
                    model.load_state_dict(torch.load(os.path.join(
                        args.model_dir, model_prior, model_file)))

                    # print(f'start evaluating {model_file}...')
                    train, valid = evaluate_mpvae(model, data, target_fair_labels, label_dist)

# python fairsoft_evaluate.py -dataset adult -latent_dim 8 -cuda 6
