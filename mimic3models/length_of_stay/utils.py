from mimic3models import metrics
from mimic3models import common_utils
from mimic3models import nn_utils
import threading
import os
import numpy as np
import random


def preprocess_chunk(data, ts, discretizer, normalizer=None):
    data = [discretizer.transform(X, end=t)[0] for (X, t) in zip(data, ts)]
    if (normalizer is not None):
        data = [normalizer.transform(X) for X in data]
    return data


class BatchGen(object):

    def __init__(self, reader, partition, discretizer, normalizer,
                 batch_size, steps, shuffle, return_names=False):
        self.reader = reader
        self.partition = partition
        self.discretizer = discretizer
        self.normalizer = normalizer
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.return_names = return_names

        if steps is None:
            self.n_examples = reader.get_number_of_examples()
            self.steps = (self.n_examples + batch_size - 1) // batch_size
        else:
            self.n_examples = steps * batch_size
            self.steps = steps

        self.chunk_size = min(1024, self.steps) * batch_size
        self.lock = threading.Lock()
        self.generator = self._generator()

    def _generator(self):
        B = self.batch_size
        while True:
            if self.shuffle:
                self.reader.random_shuffle()
            remaining = self.n_examples
            while remaining > 0:
                current_size = min(self.chunk_size, remaining)
                remaining -= current_size

                ret = common_utils.read_chunk(self.reader, current_size)
                data = ret["X"]
                ts = ret["t"]
                labels = ret["y"]
                names = ret["name"]

                data = preprocess_chunk(data, ts, self.discretizer, self.normalizer)
                data = (data, labels)
                data = common_utils.sort_and_shuffle(data, B)

                for i in range(0, current_size, B):
                    X = nn_utils.pad_zeros(data[0][i:i+B])
                    y = data[1][i:i+B]
                    y_true = np.array(y)
                    batch_names = names[i:i+B]
                    batch_ts = ts[i:i+B]

                    if self.partition == 'log':
                        y = [metrics.get_bin_log(x, 10) for x in y]
                    if self.partition == 'custom':
                        y = [metrics.get_bin_custom(x, 10) for x in y]

                    y = np.array(y)

                    if self.return_y_true:
                        batch_data = (X, y, y_true)
                    else:
                        batch_data = (X, y)

                    if not self.return_names:
                        yield batch_data
                    else:
                        yield {"data": batch_data, "names": batch_names, "ts": batch_ts}

    def __iter__(self):
        return self.generator

    def next(self, return_y_true=False):
        with self.lock:
            self.return_y_true = return_y_true
            return self.generator.next()

    def __next__(self):
        return self.generator.__next__()


class BatchGenDeepSupervision(object):

    def __init__(self, dataloader, partition, discretizer, normalizer,
                 batch_size, shuffle, return_names=False):
        self.partition = partition
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.return_names = return_names

        self._load_per_patient_data(dataloader, discretizer, normalizer)

        self.steps = (len(self.data[1]) + batch_size - 1) // batch_size
        self.lock = threading.Lock()
        self.generator = self._generator()

    def _load_per_patient_data(self, dataloader, discretizer, normalizer):
        timestep = discretizer._timestep

        def get_bin(t):
            eps = 1e-6
            return int(t / timestep - eps)

        N = len(dataloader._data["X"])
        Xs = []
        ts = []
        masks = []
        ys = []
        names = []

        for i in range(N):
            X = dataloader._data["X"][i]
            cur_ts = dataloader._data["ts"][i]
            cur_ys = dataloader._data["ys"][i]
            name = dataloader._data["name"][i]

            cur_ys = [float(x) for x in cur_ys]

            T = max(cur_ts)
            nsteps = get_bin(T) + 1
            mask = [0] * nsteps
            y = [0] * nsteps

            for pos, z in zip(cur_ts, cur_ys):
                mask[get_bin(pos)] = 1
                y[get_bin(pos)] = z

            X = discretizer.transform(X, end=T)[0]
            if (normalizer is not None):
                X = normalizer.transform(X)

            Xs.append(X)
            masks.append(np.array(mask))
            ys.append(np.array(y))
            names.append(name)
            ts.append(cur_ts)

            assert np.sum(mask) > 0
            assert len(X) == len(mask) and len(X) == len(y)

        self.data = [[Xs, masks], ys]
        self.names = names
        self.ts = ts

    def _generator(self):
        B = self.batch_size
        while True:
            if self.shuffle:
                N = len(self.data[1])
                order = range(N)
                random.shuffle(order)
                tmp_data = [[[None]*N, [None]*N], [None]*N]
                tmp_names = [None] * N
                tmp_ts = [None] * N
                for i in range(N):
                    tmp_data[0][0][i] = self.data[0][0][order[i]]
                    tmp_data[0][1][i] = self.data[0][1][order[i]]
                    tmp_data[1][i] = self.data[1][order[i]]
                    tmp_names[i] = self.names[order[i]]
                    tmp_ts[i] = self.ts[order[i]]
                self.data = tmp_data
                self.names = tmp_names
                self.ts = tmp_ts
            else:
                # sort entirely
                Xs = self.data[0][0]
                masks = self.data[0][1]
                ys = self.data[1]
                (Xs, masks, ys, self.names, self.ts) = common_utils.sort_and_shuffle([Xs, masks, ys,
                                                                                      self.names, self.ts], B)
                self.data = [[Xs, masks], ys]

            for i in range(0, len(self.data[1]), B):
                X = self.data[0][0][i:i+B]
                mask = self.data[0][1][i:i+B]
                y = self.data[1][i:i+B]
                names = self.names[i:i+B]
                ts = self.ts[i:i+B]

                y_true = [np.array(x) for x in y]
                y_true = nn_utils.pad_zeros(y_true)
                y_true = np.expand_dims(y_true, axis=-1)

                if self.partition == 'log':
                    y = [np.array([metrics.get_bin_log(x, 10) for x in z]) for z in y]
                if self.partition == 'custom':
                    y = [np.array([metrics.get_bin_custom(x, 10) for x in z]) for z in y]

                X = nn_utils.pad_zeros(X)  # (B, T, D)
                mask = nn_utils.pad_zeros(mask)  # (B, T)
                y = nn_utils.pad_zeros(y)
                y = np.expand_dims(y, axis=-1)

                if self.return_y_true:
                    batch_data = ([X, mask], y, y_true)
                else:
                    batch_data = ([X, mask], y)

                if not self.return_names:
                    yield batch_data
                else:
                    yield {"data": batch_data, "names": names, "ts": ts}

    def __iter__(self):
        return self.generator

    def next(self, return_y_true=False):
        with self.lock:
            self.return_y_true = return_y_true
            return self.generator.next()

    def __next__(self):
        return self.generator.__next__()
