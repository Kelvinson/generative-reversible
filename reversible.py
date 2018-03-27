import torch as th
import torch.nn as nn
import numpy as np
from scipy import interpolate
import matplotlib.pyplot as plt
from matplotlib import cm
from torch.autograd import Variable
import torch as th
import random
from numpy.random import RandomState


class GenerativeIterator(object):
    def __init__(self, upsample_supervised, batch_size):
        self.upsample_supervised = upsample_supervised
        self.rng = RandomState(3847634)
        self.batch_size = batch_size

    def get_batches(self, inputs, targets, inputs_u, linear_weights_u):
        i_supervised = np.arange(len(inputs))
        if inputs_u is not None:
            i_unsupervised = np.arange(len(inputs_u))
        if (inputs_u is not None) and self.upsample_supervised:
            assert inputs_u is not None
            n_missing = len(i_unsupervised) - len(i_supervised)
            while n_missing > 0:
                i_add = self.rng.choice(np.arange(len(inputs)), size=n_missing,
                                   replace=False)
                i_supervised = np.concatenate((i_supervised, i_add))
        if inputs_u is not None:
            supervised_batch_size = int(np.round(self.batch_size *
                                                 (len(i_supervised) / float(
                                                     len(i_supervised) + len(
                                                         i_unsupervised)))))
            unsupervised_batch_size = self.batch_size - supervised_batch_size
            batches_supervised = get_exact_size_batches(
                len(i_supervised), self.rng, supervised_batch_size)
            batches_unsupervised = get_exact_size_batches(
                len(i_unsupervised), self.rng, unsupervised_batch_size)
            assert len(batches_supervised) == len(batches_unsupervised)
            # else you should check which one is smaller and add the first batch as the last batch again
            # or some random examples as another batch
            for batch_i_s, batch_i_u in zip(batches_supervised,
                                            batches_unsupervised):
                batch_X_s = inputs[th.LongTensor(batch_i_s)]
                batch_X_u = inputs_u[th.LongTensor(batch_i_u)]
                batch_y_s = targets[th.LongTensor(batch_i_s)]
                batch_y_u = linear_weights_u[th.LongTensor(batch_i_u)]
                batch_y_u = th.nn.functional.softmax(batch_y_u, dim=1)
                batch_X = th.cat((batch_X_s, batch_X_u), dim=0)
                batch_y = th.cat((batch_y_s, batch_y_u), dim=0)
                yield batch_X, batch_y
        else:
            supervised_batch_size = self.batch_size
            batches_supervised = get_exact_size_batches(
                len(i_supervised), self.rng, supervised_batch_size)
            for batch_i_s in batches_supervised:
                batch_X_s = inputs[th.LongTensor(batch_i_s)]
                batch_y_s = targets[th.LongTensor(batch_i_s)]
                yield batch_X_s, batch_y_s


class GenerativeRevTrainer(object):
    def __init__(self, model, optimizer, means_per_dim, stds_per_dim,
                 iterator):
        self.__dict__.update(locals())
        del self.self
        self.rng = RandomState(394834)

    def train_epoch(self, inputs, targets, inputs_u, linear_weights_u,
                    trans_loss_function,
                    directions_adv, n_dir_matrices=1):
        loss = 0
        n_examples = 0
        for batch_X, batch_y in self.iterator.get_batches(
                inputs, targets, inputs_u, linear_weights_u):
            dir_mats = [sample_directions(self.means_per_dim.size()[1], True,
                                          cuda=False)
                        for _ in range(n_dir_matrices)]
            directions = th.cat(dir_mats, dim=0)
            if directions_adv is not None:
                directions = th.cat((directions, directions_adv), dim=0)
            #            directions = sample_directions(self.means_per_dim.size()[1], True, cuda=False)
            batch_loss = train_on_batch(batch_X, self.model, self.means_per_dim,
                                        self.stds_per_dim,
                                        batch_y, self.optimizer, directions,
                                        trans_loss_function)
            loss = loss + batch_loss * len(batch_X)
            n_examples = n_examples + batch_X.size()[0]
        mean_loss = var_to_np(loss / n_examples)[0]
        return mean_loss


def train_on_batch(batch_X, model, means_per_dim, stds_per_dim, soft_targets,
                   optimizer,
                   directions, trans_loss_function,
                   ):
    if means_per_dim.is_cuda:
        batch_X = batch_X.cuda()
        soft_targets = soft_targets.cuda()

    batch_outs = model(batch_X)
    trans_loss = trans_loss_function(batch_outs, directions, soft_targets,
                                     means_per_dim, stds_per_dim)
    optimizer.zero_grad()
    trans_loss.backward()
    optimizer.step()
    return trans_loss


def w2_both(outs, directions, soft_targets,
            means_per_dim, stds_per_dim):
    directions = norm_and_var_directions(directions)
    projected_samples = th.mm(outs, directions.t())
    sorted_samples, i_sorted = th.sort(projected_samples, dim=0)
    loss = 0
    for i_cluster in range(len(means_per_dim)):
        this_means = means_per_dim[i_cluster:i_cluster + 1]
        this_stds = stds_per_dim[i_cluster:i_cluster + 1]
        this_weights = soft_targets[:, i_cluster]
        sorted_weights = th.stack([this_weights[i_sorted[:, i_dim]]
                                   for i_dim in range(i_sorted.size()[1])],
                                  dim=1)
        all_i_cdfs = compute_all_i_cdfs(this_means, this_stds, sorted_weights,
                                        directions)

        diffs = all_i_cdfs - sorted_samples
        # why times weights and not times probs?
        this_loss = th.sqrt(th.mean(diffs * diffs * sorted_weights))
        loss = loss + this_loss
    return loss


def w2_both_demeaned_phases(outs, directions, soft_targets,
            means_per_dim, stds_per_dim, gaussianize_phases):
    loss = 0
    directions = norm_and_var_directions(directions)
    for i_cluster in range(len(means_per_dim)):
        this_means = means_per_dim[i_cluster:i_cluster + 1]
        this_stds = stds_per_dim[i_cluster:i_cluster + 1]
        this_weights = soft_targets[:, i_cluster]
        this_outs = set_phase_interval_around_mean_in_outs(
            outs, means=this_means.squeeze(0))
        if gaussianize_phases:
            this_outs = uniform_to_gaussian_phases_in_outs(
                this_outs,  means=this_means.squeeze(0))
        projected_samples = th.mm(this_outs, directions.t())
        sorted_samples, i_sorted = th.sort(projected_samples, dim=0)
        sorted_weights = th.stack([this_weights[i_sorted[:, i_dim]]
                                   for i_dim in range(i_sorted.size()[1])],
                                  dim=1)
        all_i_cdfs = compute_all_i_cdfs(this_means, this_stds, sorted_weights,
                                        directions)

        diffs = all_i_cdfs - sorted_samples
        this_loss = th.sqrt(th.mean(diffs * diffs * sorted_weights))
        loss = loss + this_loss
    return loss



def standard_gaussian_icdf(cdfs):
    # see https://en.wikipedia.org/wiki/Normal_distribution#Quantile_function
    return th.erfinv(2 * cdfs - 1) * np.sqrt(2.0)


def standard_gaussian_cdf(vals):
    #see https://en.wikipedia.org/wiki/Normal_distribution#Cumulative_distribution_function
    return 0.5 * (1 + th.erf(vals / (np.sqrt(2))))


def radial_dist_per_cluster(outs, soft_targets, means_per_dim, stds_per_dim,
                          demean_phases=False, gaussianize_phases=False,
                          n_samples=100, adv_samples=None,
                            n_interpolation_samples='n_outs',
                            weight_by_expected_diffs=True):
    loss = 0
    for i_cluster in range(len(means_per_dim)):
        this_outs = outs
        this_means = means_per_dim[i_cluster]
        this_stds = stds_per_dim[i_cluster]
        this_weights = soft_targets[:, i_cluster]
        if demean_phases:
            this_outs = set_phase_interval_around_mean_in_outs(
                this_outs, means=this_means.squeeze(0).detach())
            if gaussianize_phases:
                this_outs = uniform_to_gaussian_phases_in_outs(
                    this_outs, means=this_means.squeeze(0))
        else:
            assert not gaussianize_phases
        this_loss = radial_distance_loss(
            this_outs, this_weights, this_means, this_stds, n_samples=n_samples,
            adv_samples=adv_samples, n_interpolation_samples=n_interpolation_samples,
            weight_by_expected_diffs=weight_by_expected_diffs)

        loss = loss + this_loss
    return loss


def radial_distance_loss(outs, weights, mean, std, n_samples=100,
                         adv_samples=None,
                         n_interpolation_samples='n_outs',
                         weight_by_expected_diffs=True):
    if n_interpolation_samples == 'n_outs':
        n_interpolation_samples = len(outs)
    orig_samples = th.autograd.Variable(th.randn(n_samples, len(mean)))
    samples = (orig_samples * std.unsqueeze(0)) + mean.unsqueeze(0)
    if adv_samples is not None:
        samples = th.cat((samples, adv_samples), dim=0)
    # compute expected w2 distance per sample
    # to weight the loss
    demeaned_samples = samples - mean.unsqueeze(0)
    noncentrality_per_sample = th.sum((demeaned_samples *demeaned_samples), dim=1)
    expected_diffs = th.sqrt(noncentrality_per_sample + th.sum(std * std).unsqueeze(0))
    expected_diffs = expected_diffs / th.sum(expected_diffs)
    if not weight_by_expected_diffs:
        expected_diffs = expected_diffs * 0 + 1
    diffs = outs.unsqueeze(0) - samples.unsqueeze(1)
    diffs = th.sum((diffs * diffs), dim=2)


    sorted_diffs, i_sorted = th.sort(diffs, dim=1)
    n_virtual_samples = th.sum(weights)
    start = 1 / (2 * n_virtual_samples)
    wanted_sum = 1 - (2 / (n_virtual_samples))
    probs = weights * wanted_sum / n_virtual_samples

    sorted_probs = th.stack([probs[i_sorted[i_sample]]
                             for i_sample in range(len(samples))],
                            dim=0)
    empirical_cdf = start + th.cumsum(sorted_probs, dim=1)

    ref_samples = (th.autograd.Variable(
        th.randn(n_interpolation_samples, len(mean))) * std.unsqueeze(0)) + (
                      mean.unsqueeze(0))
    reference_diffs = ref_samples.unsqueeze(0) - samples.unsqueeze(1)
    reference_diffs = th.sum((reference_diffs * reference_diffs), dim=2)
    ref_sorted_diffs, _ = th.sort(reference_diffs, dim=1)
    grid_x = (empirical_cdf - 0.5) * 2
    # samples x reference samples
    grid_y = grid_x.detach() * 0
    grid = th.stack((grid_y, grid_x), dim=2).unsqueeze(2)
    # samples x reference samples x 1 x 2
    ref_interpolated_diffs = th.nn.functional.grid_sample(
        ref_sorted_diffs.unsqueeze(1).unsqueeze(3), grid)
    ref_interpolated_diffs = ref_interpolated_diffs.squeeze(3).squeeze(1)
    diff_diff = th.abs(ref_interpolated_diffs - sorted_diffs)
    # still samples x outs
    # probs simple without changing for cdf wanted sum...
    probs_simple = weights / th.sum(weights)
    sorted_probs_simple = th.stack([probs_simple[i_sorted[i_sample]]
                                    for i_sample in range(len(samples))],
                                   dim=0)
    diff_diff = diff_diff * sorted_probs_simple
    # sum over weighted outs, then norm by expected diff
    loss = th.mean(th.sqrt(th.sum(diff_diff, dim=1)) / expected_diffs)
    return loss


def uniform_to_gaussian_phases_in_outs(outs_with_demeaned_phases, means):
    n_chans = outs_with_demeaned_phases.size()[1]
    assert n_chans % 2 == 0
    amps = outs_with_demeaned_phases[:, :n_chans // 2]
    phases = outs_with_demeaned_phases[:, n_chans // 2:]
    mean_phases = means[n_chans // 2:]
    gauss_phases = uniform_to_gaussian_phases(phases, mean_phases)
    outs = th.cat((amps, gauss_phases), dim=1)
    return outs


def uniform_to_gaussian_phases(phases, mean_phases):
    eps = 1e-6#1e-7#1e-7 # 1e-8 results in -inf for icdf of gaussian.....
    start = mean_phases - np.pi - eps
    stop = mean_phases + np.pi + eps
    cdfs = (phases - start.unsqueeze(0)) / (stop - start).unsqueeze(0)
    icdfs = standard_gaussian_icdf(cdfs)
    return (icdfs * (stop - start).unsqueeze(0)) + mean_phases.unsqueeze(0)


def gaussian_to_uniform_phases_in_outs(outs_with_gaussian_phases, means):
    n_chans = outs_with_gaussian_phases.size()[1]
    assert n_chans % 2 == 0
    amps = outs_with_gaussian_phases[:, :n_chans // 2]
    phases = outs_with_gaussian_phases[:, n_chans // 2:]
    mean_phases = means[n_chans // 2:]
    uni_phases = gaussian_to_uniform_phases(phases, mean_phases)
    outs = th.cat((amps, uni_phases), dim=1)
    return outs


def gaussian_to_uniform_phases(phases, mean_phases):
    eps = 1e-6#1e-7#1e-7 # 1e-8 results in -inf for icdf of gaussian.....
    start = mean_phases - np.pi - eps
    stop = mean_phases + np.pi + eps
    icdfs = (phases - mean_phases.unsqueeze(0)) / (stop - start).unsqueeze(0)
    cdfs = standard_gaussian_cdf(icdfs)
    uni_phases = (cdfs * (stop - start).unsqueeze(0)) + start.unsqueeze(0)
    return uni_phases


def compute_all_i_cdfs(this_means, this_stds, sorted_weights, directions):
    transformed_means, transformed_stds = transform_gaussian_by_dirs(
        this_means, th.abs(this_stds), directions)

    n_virtual_samples = th.sum(sorted_weights[:, 0])
    start = 1 / (2 * n_virtual_samples)
    wanted_sum = 1 - (2 / (n_virtual_samples))
    probs = sorted_weights * wanted_sum / n_virtual_samples
    empirical_cdf = start + th.cumsum(probs, dim=0)

    # see https://en.wikipedia.orsorted_softmaxedg/wiki/Normal_distribution -> Quantile function
    i_cdf = th.autograd.Variable(
        th.FloatTensor([np.sqrt(2.0)])) * th.erfinv(
        2 * empirical_cdf - 1)
    i_cdf = i_cdf.squeeze()
    all_i_cdfs = i_cdf * transformed_stds.t() + transformed_means.t()
    return all_i_cdfs


def get_balanced_batches(n_trials, rng, shuffle, n_batches=None,
                         batch_size=None):
    """Create indices for batches balanced in size
    (batches will have maximum size difference of 1).
    Supply either batch size or number of batches. Resulting batches
    will not have the given batch size but rather the next largest batch size
    that allows to split the set into balanced batches (maximum size difference 1).

    Parameters
    ----------
    n_trials : int
        Size of set.
    rng : RandomState

    shuffle : bool
        Whether to shuffle indices before splitting set.
    n_batches : int, optional
    batch_size : int, optional

    Returns
    -------

    """
    assert batch_size is not None or n_batches is not None
    if n_batches is None:
        n_batches = int(np.round(n_trials / float(batch_size)))

    if n_batches > 0:
        min_batch_size = n_trials // n_batches
        n_batches_with_extra_trial = n_trials % n_batches
    else:
        n_batches = 1
        min_batch_size = n_trials
        n_batches_with_extra_trial = 0
    assert n_batches_with_extra_trial < n_batches
    all_inds = np.array(range(n_trials))
    if shuffle:
        rng.shuffle(all_inds)
    i_start_trial = 0
    i_stop_trial = 0
    batches = []
    for i_batch in range(n_batches):
        i_stop_trial += min_batch_size
        if i_batch < n_batches_with_extra_trial:
            i_stop_trial += 1
        batch_inds = all_inds[range(i_start_trial, i_stop_trial)]
        batches.append(batch_inds)
        i_start_trial = i_stop_trial
    assert i_start_trial == n_trials
    return batches


def set_random_seeds(seed, cuda):
    """
    Set seeds for python random module numpy.random and torch.

    Parameters
    ----------
    seed: int
        Random seed.
    cuda: bool
        Whether to set cuda seed with torch.

    """
    random.seed(seed)
    th.manual_seed(seed)
    if cuda:
        th.cuda.manual_seed_all(seed)
    np.random.seed(seed)


def np_to_var(X, requires_grad=False, dtype=None, pin_memory=False,
              **var_kwargs):
    """
    Convenience function to transform numpy array to `torch.autograd.Variable`.

    Converts `X` to ndarray using asarray if necessary.

    Parameters
    ----------
    X: ndarray or list or number
        Input arrays
    requires_grad: bool
        passed on to Variable constructor
    dtype: numpy dtype, optional
    var_kwargs:
        passed on to Variable constructor

    Returns
    -------
    var: `torch.autograd.Variable`
    """
    if not hasattr(X, '__len__'):
        X = [X]
    X = np.asarray(X)
    if dtype is not None:
        X = X.astype(dtype)
    X_tensor = th.from_numpy(X)
    if pin_memory:
        X_tensor = X_tensor.pin_memory()
    return Variable(X_tensor, requires_grad=requires_grad, **var_kwargs)


def var_to_np(var):
    """Convenience function to transform `torch.autograd.Variable` to numpy
    array.

    Should work both for CPU and GPU."""
    return var.cpu().data.numpy()


### Reversible model parts

class ReversibleBlock(th.nn.Module):
    def __init__(self, F, G):
        super(ReversibleBlock, self).__init__()
        self.F = F
        self.G = G

    def forward(self, x):
        n_chans = x.size()[1]
        assert n_chans % 2 == 0
        x1 = x[:, :n_chans // 2]
        x2 = x[:, n_chans // 2:]
        y1 = self.F(x1) + x2
        y2 = self.G(y1) + x1
        return th.cat((y1, y2), dim=1)


class SubsampleSplitter(th.nn.Module):
    def __init__(self, stride, chunk_chans_first=True):
        super(SubsampleSplitter, self).__init__()
        if not hasattr(stride, '__len__'):
            stride = (stride, stride)
        self.stride = stride
        self.chunk_chans_first = chunk_chans_first

    def forward(self, x):
        # Chunk chans first to ensure that each of the two streams in the
        # reversible network will see a subsampled version of the whole input
        # (in case the preceding blocks would not alter the input)
        # and not one half of the input
        new_x = []
        if self.chunk_chans_first:
            xs = th.chunk(x, 2, dim=1)
        else:
            xs = [x]
        for one_x in xs:
            for i_stride in range(self.stride[0]):
                for j_stride in range(self.stride[1]):
                    new_x.append(
                        one_x[:, :, i_stride::self.stride[0],
                        j_stride::self.stride[1]])
        new_x = th.cat(new_x, dim=1)
        return new_x


def invert(feature_model, features):
    if feature_model.__class__.__name__ == 'ReversibleBlock' or feature_model.__class__.__name__ == 'SubsampleSplitter':
        feature_model = nn.Sequential(feature_model, )
    for module in reversed(list(feature_model.children())):
        if module.__class__.__name__ == 'ReversibleBlock':
            n_chans = features.size()[1]
            # y1 = self.F(x1) + x2
            # y2 = self.G(y1) + x1
            y1 = features[:, :n_chans // 2]
            y2 = features[:, n_chans // 2:]

            x1 = y2 - module.G(y1)
            x2 = y1 - module.F(x1)
            features = th.cat((x1, x2), dim=1)
        if module.__class__.__name__ == 'SubsampleSplitter':
            # after splitting the input into two along channel dimension if possible
            # for i_stride in range(self.stride):
            #    for j_stride in range(self.stride):
            #        new_x.append(one_x[:,:,i_stride::self.stride, j_stride::self.stride])
            n_all_chans_before = features.size()[1] // (
            module.stride[0] * module.stride[1])
            # if ther was only one chan before, chunk had no effect
            if module.chunk_chans_first and (n_all_chans_before > 1):
                chan_features = th.chunk(features, 2, dim=1)
            else:
                chan_features = [features]
            all_previous_features = []
            for one_chan_features in chan_features:
                previous_features = th.zeros(one_chan_features.size()[0],
                                             one_chan_features.size()[1] // (
                                             module.stride[0] * module.stride[
                                                 1]),
                                             one_chan_features.size()[2] *
                                             module.stride[0],
                                             one_chan_features.size()[3] *
                                             module.stride[1])
                if features.is_cuda:
                    previous_features = previous_features.cuda()
                previous_features = th.autograd.Variable(previous_features)

                n_chans_before = previous_features.size()[1]
                cur_chan = 0
                for i_stride in range(module.stride[0]):
                    for j_stride in range(module.stride[1]):
                        previous_features[:, :, i_stride::module.stride[0],
                        j_stride::module.stride[1]] = (
                            one_chan_features[:,
                            cur_chan * n_chans_before:cur_chan * n_chans_before + n_chans_before])
                        cur_chan += 1
                all_previous_features.append(previous_features)
            features = th.cat(all_previous_features, dim=1)
        if module.__class__.__name__ == 'AmplitudePhase':
            n_chans = features.size()[1]
            assert n_chans % 2 == 0
            amps = features[:, :n_chans // 2]
            phases = features[:, n_chans // 2:]
            x1, x2 = amp_phase_to_x_y(amps, phases)
            features = th.cat((x1, x2), dim=1)
    return features


def init_std_mean(feature_model, inputs, targets, means_per_dim, stds_per_dim,
                  set_phase_interval):
    outs = feature_model(inputs)
    for i_cluster in range(len(means_per_dim)):
        n_elems = len(th.nonzero(targets[:, i_cluster] == 1))
        this_weights = targets[:, i_cluster]
        if set_phase_interval:
            this_outs = set_phase_interval_around_mean_in_outs(
                outs, this_weights=this_weights)

        this_outs = this_outs[(this_weights == 1).unsqueeze(1)].resize(
            n_elems, outs.size()[1])

        means = th.mean(this_outs, dim=0)
        # this_outs = uniform_to_gaussian_phases_in_outs(this_outs,means)
        stds = th.std(this_outs, dim=0)
        means_per_dim.data[i_cluster] = means.data
        stds_per_dim.data[i_cluster] = stds.data


class AmplitudePhase(th.nn.Module):
    def __init__(self):
        super(AmplitudePhase, self).__init__()

    def forward(self, x):
        n_chans = x.size()[1]
        assert n_chans % 2 == 0
        x1 = x[:, :n_chans // 2]
        x2 = x[:, n_chans // 2:]
        amps, phases = to_amp_phase(x1, x2)
        return th.cat((amps, phases), dim=1)


def to_amp_phase(x, y):
    amps = th.sqrt((x * x) + (y * y))
    phases = th.atan2(y, x)
    return amps, phases


def amp_phase_to_x_y(amps, phases):
    x, y = th.cos(phases), th.sin(phases)

    x = x * amps
    y = y * amps
    return x, y



def compute_mean_phase(phases, this_weights):
    x, y = th.cos(phases), th.sin(phases)
    this_probs = this_weights / th.sum(this_weights)
    mean_x = th.sum(this_probs.unsqueeze(1) * x, dim=0)
    mean_y = th.sum(this_probs.unsqueeze(1) * y, dim=0)
    mean_phase = th.atan2(mean_y, mean_x)
    return mean_phase


def set_phase_interval_around_mean(phases, mean_phase):
    mean_phase = mean_phase.unsqueeze(0)
    return ((phases - mean_phase + np.pi) % (2 * np.pi)) - np.pi + mean_phase


def set_phase_interval_around_mean_in_outs(outs, this_weights=None, means=None,):
    assert (this_weights is None) != (means is None)
    n_chans = outs.size()[1]
    assert n_chans % 2 == 0
    amps = outs[:, :n_chans // 2]
    phases = outs[:, n_chans // 2:]
    if this_weights is not None:
        mean_phase = compute_mean_phase(phases, this_weights)
    else:
        assert means.size()[0] == n_chans
        mean_phase = means[n_chans // 2:]
    demeaned_phases = set_phase_interval_around_mean(phases, mean_phase)
    new_outs = th.cat((amps, demeaned_phases), dim=1)
    return new_outs


def get_inputs_from_reverted_samples(n_inputs, means_per_dim, stds_per_dim,
                                     weights_per_cluster,
                                     feature_model,
                                     to_4d=True,
                                     gaussian_to_uniform_phases=False):
    feature_model.eval()
    sizes = sizes_from_weights(n_inputs, var_to_np(weights_per_cluster))
    gauss_samples = sample_mixture_gaussian(sizes, means_per_dim, stds_per_dim)
    if to_4d:
        gauss_samples = gauss_samples.unsqueeze(2).unsqueeze(3)
    if gaussian_to_uniform_phases:
        assert len(means_per_dim) == 1
        gauss_samples = gaussian_to_uniform_phases_in_outs(
            gauss_samples, means_per_dim.squeeze(0))
    rec_var = invert(feature_model, gauss_samples)
    rec_examples = var_to_np(rec_var).squeeze()
    return rec_examples, gauss_samples


def weights_init(module, conv_weight_init_fn):
    classname = module.__class__.__name__
    if (('Conv' in classname) or (
                'Linear' in classname)) and classname != "AvgPool2dWithConv":
        conv_weight_init_fn(module.weight)
        if module.bias is not None:
            th.nn.init.constant(module.bias, 0)
    elif 'BatchNorm' in classname:
        th.nn.init.constant(module.weight, 1)
        th.nn.init.constant(module.bias, 0)


def init_model_params(feature_model, gain):
    feature_model.apply(lambda module: weights_init(
        module,
        lambda w: th.nn.init.xavier_uniform(w, gain=gain)))


## Sampling gaussian mixture

def sample_mixture_gaussian(sizes_per_cluster, means_per_dim, stds_per_dim):
    # assume mean/std are clusters x dims
    parts = []
    n_dims = means_per_dim.size()[1]
    for n_samples, mean, std in zip(sizes_per_cluster, means_per_dim,
                                    stds_per_dim):
        if n_samples == 0: continue
        assert n_samples > 0
        samples = th.randn(n_samples, n_dims)
        samples = th.autograd.Variable(samples)
        if std.is_cuda:
            samples = samples.cuda()
        samples = samples * std.unsqueeze(0) + mean.unsqueeze(0)
        parts.append(samples)
    all_samples = th.cat(parts, dim=0)
    return all_samples


def sizes_from_weights(size, weights, ):
    weights = weights / np.sum(weights)
    fractional_sizes = weights * size

    rounded = np.int64(np.round(fractional_sizes))
    diff_with_half = (fractional_sizes % 1) - 0.5

    n_total = np.sum(rounded)
    # Those closest to 0.5 rounded, take next biggest or next smallest number
    # to match wanted overall size
    while n_total > size:
        mask = (diff_with_half > 0) & (rounded > 0)
        if np.sum(mask) == 0:
            mask = rounded > 0
        i_min = np.argsort(diff_with_half[mask])[0]
        i_min = np.flatnonzero(mask)[i_min]
        diff_with_half[i_min] += 0.5
        rounded[i_min] -= 1
        n_total -= 1
    while n_total < size:
        mask = (diff_with_half < 0) & (rounded > 0)
        if np.sum(mask) == 0:
            mask = rounded > 0
        i_min = np.argsort(-diff_with_half[mask])[0]
        i_min = np.flatnonzero(mask)[i_min]
        diff_with_half[i_min] -= 0.5
        rounded[i_min] += 1
        n_total += 1

    assert np.sum(rounded) == size
    # pytorch needs list of int
    sizes = [int(s) for s in rounded]
    return sizes


def sample_directions(n_dims, orthogonalize, cuda):
    directions = th.randn(n_dims, n_dims)
    if orthogonalize:
        directions, _ = th.qr(directions)

    if cuda:
        directions = directions.cuda()
    directions = th.autograd.Variable(directions, requires_grad=False)
    norm_factors = th.norm(directions, p=2, dim=1, keepdim=True)
    directions = directions / norm_factors
    return directions


def norm_and_var_directions(directions):
    if th.is_tensor(directions):
        directions = th.autograd.Variable(directions, requires_grad=False)
    norm_factors = th.norm(directions, p=2, dim=1, keepdim=True)
    directions = directions / norm_factors
    return directions


def transform_gaussian_by_dirs(means, stds, directions):
    # directions is directions x dims
    # means is clusters x dims
    # stds is clusters x dims
    transformed_means = th.mm(means, directions.transpose(1, 0)).transpose(1, 0)
    # transformed_means is now
    # directions x clusters
    stds_for_dirs = stds.transpose(1, 0).unsqueeze(0)  # 1 x dims x clusters
    transformed_stds = th.sqrt(th.sum(
        (directions * directions).unsqueeze(2) *
        (stds_for_dirs * stds_for_dirs),
        dim=1))
    # transformed_stds is now
    # directions x clusters
    return transformed_means, transformed_stds


def ensure_on_same_device(*variables):
    any_cuda = np.any([v.is_cuda for v in variables])
    if any_cuda:
        variables = [ensure_cuda(v) for v in variables]
    return variables


def ensure_cuda(v):
    if not v.is_cuda:
        v = v.cuda()
    return v


def get_exact_size_batches(n_trials, rng, batch_size):
    i_trials = np.arange(n_trials)
    rng.shuffle(i_trials)
    i_trial = 0
    batches = []
    for i_trial in range(0, n_trials - batch_size, batch_size):
        batches.append(i_trials[i_trial: i_trial + batch_size])
    i_trial = i_trial + batch_size

    last_batch = i_trials[i_trial:]
    n_remain = batch_size - len(last_batch)
    last_batch = np.concatenate((last_batch, i_trials[:n_remain]))
    batches.append(last_batch)
    return batches


def get_batches_equal_classes(targets, n_classes, rng, batch_size):
    batches_per_cluster = []
    for i_cluster in range(n_classes):
        n_examples = np.sum(targets == i_cluster)
        examples_per_batch = get_exact_size_batches(
            n_examples, rng, batch_size)
        this_cluster_indices = np.nonzero(targets == i_cluster)[0]
        examples_per_batch = [this_cluster_indices[b] for b in
                              examples_per_batch]
        # revert back to actual indices
        batches_per_cluster.append(examples_per_batch)

    batches = np.concatenate(batches_per_cluster, axis=1)
    return batches



def plot_xyz(x, y, z):
    fig = plt.figure(figsize=(5, 5))
    ax = plt.gca()
    xx = np.linspace(min(x), max(x), 100)
    yy = np.linspace(min(y), max(y), 100)
    f = interpolate.NearestNDInterpolator(list(zip(x, y)), z)
    assert len(xx) == len(yy)
    zz = np.ones((len(xx), len(yy)))
    for i_x in range(len(xx)):
        for i_y in range(len(yy)):
            # somehow this is correct. don't know why :(
            zz[i_y, i_x] = f(xx[i_x], yy[i_y])
    assert not np.any(np.isnan(zz))

    ax.imshow(zz, vmin=-np.max(np.abs(z)), vmax=np.max(np.abs(z)), cmap=cm.PRGn,
              extent=[min(x), max(x), min(y), max(y)], origin='lower',
              interpolation='nearest', aspect='auto')



