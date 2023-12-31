import torch as _torch
import torch.nn as _nn
from torch import Tensor
import torchmetrics as _torchmetrics

import time as _time
import pandas as _pd            # type: ignore
import os as _os
from copy import deepcopy as _deepcopy
import numpy as _np
import gc as _gc
from collections import defaultdict as _defaultdict  # type: ignore

from .utils import _diffopt_state_dict, _divide_chunks, _imshow, _cycle
import higher as _higher        # type: ignore

import typing as _typing


class GTN:
    ''' Base GTN Class that sets up architecture.
    '''
    def __init__(self,
                 loss_fn: _typing.Callable[[_torch.Tensor, _torch.Tensor], _torch.Tensor],
                 learnerlist: _typing.List[_torch.nn.Module],
                 metrics: _typing.Union[_torchmetrics.MetricCollection, _torchmetrics.Metric],
                 num_classes: int,
                 batch_size: int = 4,
                 plot_steps: int = 25,
                 device: _typing.Optional[_torch.device] = None) -> None:
        r""" Initializes the structure of the GTN

        Parameters:
            loss_fn: Loss function for the GTN training ``learnerlist``
            metrics: torchmetrics Collection or Metric
            learnerlist: A list of ``torch.nn.Module``
            num_classes: Number of classes in the data
            batch_size: Number of ``inner_loop_steps`` to run before training on ``train_data``
            plot_steps: Number of trained models after which learned data is printed
            device: PyTorch device to run ono either ``gpu`` or ``cpu``
        """

        self.device = device
        self.learnerlist = learnerlist
        self.loss_fn = loss_fn

        self.num_classes = num_classes
        self.inner_loop_iterations: int
        self.plot_steps = plot_steps
        self.batch_size = batch_size

        self.metrics = metrics.to(self.device)
        for i in self.metrics.children():
            i.to(device)
        if isinstance(self.metrics, _torchmetrics.Metric):
            self.metrics = _torchmetrics.MetricCollection(self.metrics).to(self.device)
        self.metrics = metrics.to(self.device)

        self.params_to_train: _typing.List[_typing.Any]
        self._override: _typing.List[_typing.Any]
        self.override_params: _typing.Dict[str, _typing.Any]
        self.outer_opt: _typing.Callable[[_typing.Any], _torch.optim.Optimizer]  # Reference to an optimizer
        self.outer_opt_params: _typing.Optional[_typing.Dict[str, _typing.Any]]
        self.inner_opt: _typing.Callable[[_typing.Any], _torch.optim.Optimizer]  # Reference to an optimizer
        self.inner_opt_params: _typing.Optional[_typing.Dict[str, _typing.Any]]

    def train_on_batch(self,
                       data: _torch.Tensor,
                       labels: _torch.Tensor,
                       model: _torch.nn.Module,
                       metric: _typing.Union[_torchmetrics.MetricCollection, _torchmetrics.Metric]) -> _typing.Tuple[_torch.Tensor, _typing.Any]:
        ''' Returns loss and metric which is derived from input data and labels
        '''
        data, labels = data.to(self.device), labels.to(self.device)
        output = model(data)
        metric.update(output, labels)
        inner_loss = self.loss_fn(output, labels)
        return inner_loss, metric

    def _modify_metric(self,
                       _kind: str,
                       _metric: _typing.Dict[_typing.Any, _typing.Any]):  # TODO
        return {_kind+key: _np.round(value.item(), 3) for key, value in _metric.items()}

    def train(self,
              train_loader: _typing.Collection[_torch.Tensor],
              test_loader: _typing.Collection[_torch.Tensor],
              path: str = './gtn',
              epochs: int = 3) -> _pd.DataFrame:
        """ A function for training the GTN
        Parameters:
            train_data: PyTorch DataLoader (or iterable) for outer loop training
            test_data: PyTorch DataLoader (or iterable) for outer loop validation
            path: Path where trained Learners are stored
            epochs: Number of epochs to train a learner
        Returns:
            history: Pandas DataFrame consisting of the history and paths to learners
        """
        self._batches = list(_divide_chunks(_np.arange(self.inner_loop_iterations), self.batch_size))

        self.epochs = epochs
        self.path = path

        self._steps_per_epoch = len(train_loader)
        self.train_loader = iter(_cycle(train_loader))
        self.test_loader = iter(_cycle(test_loader))

        _inner_metrics = _deepcopy(self.metrics)
        _train_metrics = _deepcopy(self.metrics)
        _test_metrics = _deepcopy(self.metrics)

        df = _pd.DataFrame()
        if not _os.path.exists(self.path):
            _os.makedirs(self.path)

        _then = _time.time()
        for it in range(len(self.learnerlist)):
            _learner = _deepcopy(self.learnerlist[it]).to(self.device)
            _inner_optim = self.inner_opt(_learner.parameters(), **self.inner_opt_params)
            _info: _typing.Union[_typing.DefaultDict[str, _typing.Any], _typing.Dict[str, _typing.Any]] = _defaultdict(list)

            for _ in range(self.epochs):
                for _batchset in self._batches:
                    self.outer_optim.zero_grad(set_to_none=True)
                    _train_data, _train_target = next(self.train_loader)
                    _test_data, _test_target = next(self.test_loader)

                    with _higher.innerloop_ctx(_learner,
                                               _inner_optim,
                                               override={key: [value]
                                                         for key, value in zip(list(self.override_params.keys()),
                                                                               self._override)}) as (_flearner, _diffopt):

                        for step in _batchset:
                            _inner_data, _inner_target = self.get_innerloop_data(step)
                            _inner_loss, _inner_metrics = self.train_on_batch(_inner_data, _inner_target, _flearner, _inner_metrics)
                            _diffopt.step(_inner_loss)

                        _train_loss, _train_metrics = self.train_on_batch(_train_data, _train_target, _flearner, _train_metrics)
                        _train_loss.backward()
                        self.outer_optim.step()

                        _learner.load_state_dict(_flearner.state_dict())
                        _inner_optim.load_state_dict(_diffopt_state_dict(_diffopt))

                    with _torch.no_grad():
                        _learner.eval()
                        _test_loss, _test_metrics = self.train_on_batch(_test_data, _test_target, _learner, _test_metrics)

                        _innermetrics = self._modify_metric("Inner", _inner_metrics.compute())
                        _trainmetrics = self._modify_metric("Train", _train_metrics.compute())
                        _testmetrics = self._modify_metric("Test", _test_metrics.compute())

                        for m in _innermetrics.keys():
                            _info[m].append(_innermetrics[m])
                        _info['InnerLoss'].append(_np.round(_inner_loss.item(), 3))
                        for m in _trainmetrics.keys():
                            _info[m].append(_trainmetrics[m])
                        _info['TrainLoss'].append(_np.round(_train_loss.item(), 3))
                        for m in _testmetrics.keys():
                            _info[m].append(_testmetrics[m])
                        _info['TestLoss'].append(_np.round(_test_loss.item(), 3))
                        _inner_metrics.reset()
                        _train_metrics.reset()
                        _test_metrics.reset()

                print("E:", it//self._steps_per_epoch,
                      "\tB:", it % self._steps_per_epoch,
                      "\t ", {key: value[-1] for key, value in _info.items()},
                      "  \tIT: ", (it+1),
                      sep="")
            _info = {key: [value] for key, value in _info.items()}
            print()
            _inner_optim.zero_grad()
            _checkpoint = {'model': _learner, 'optimizer': _inner_optim.state_dict()}
            _info["Path"] = f'{self.path}/{it}.pth'
            _torch.save(_checkpoint, _info['Path'])
            df = _pd.concat([df, _pd.DataFrame(_info)], ignore_index=True)
            if (it + 1) % self.plot_steps == 0:
                _imshow(_inner_data)

        del _train_loss, _inner_loss, _test_loss, _train_data, _inner_data, _test_data, _learner, _inner_optim
        _gc.collect()
        _torch.cuda.empty_cache()

        _now = _time.time()
        # curriculum_data.requires_grads=False

        print("\n\nTotal Time Taken: ", _now-_then, "\t Average Time: ", (_now-_then)/len(self.learnerlist))
        return df

    def compileoptimizer(self,
                         inner_opt: _typing.Any = _torch.optim.SGD,
                         inner_opt_params: _typing.Dict[str, _typing.Any] = {'lr': 0.01},
                         override_params: _typing.Dict[str, _typing.Any] = {'lr': 0.02, 'momentum': 0.9},
                         outer_opt: _typing.Any = _torch.optim.Adam,
                         outer_opt_params: _typing.Dict[str, _typing.Any] = {'lr': 0.01, 'betas': (0.9, 0.9)}
                         ) -> None:
        """ Compiles the outer loop optimizer reference ``outer_opt``
        Parameters:
            inner_opt: Reference to an optimizer for inner loop training
            inner_opt_params: Dictionary of ``inner_opt`` parameters and corresponding values
            override_params: Dictionary of ``inner_opt`` parameters that need to be trained
            outer_opt: Reference to an optimizer for outer loop meta learning
            outer_opt_params: Dictionary of ``inner_opt`` parameters and corresponding values
        """
        self.override_params = override_params
        self.outer_opt = outer_opt
        self.outer_opt_params = outer_opt_params
        self.inner_opt = inner_opt
        self.inner_opt_params = inner_opt_params

        self._override = [_nn.Parameter(Tensor(x).to(self.device)) if isinstance(x, list)
                          else _nn.Parameter(Tensor([x]).to(self.device)) for x in self.override_params.values()]

        self.params_to_train += self._override
        self.outer_optim = self.outer_opt(self.params_to_train, **self.outer_opt_params)

    def get_innerloop_data(self, step) -> _typing.Tuple[_torch.Tensor, _torch.Tensor]:
        """ Function is overriden by base class that provides data to the Learner for inner loop training
        Parameters:
            None
        Returns:
            None
        """
        raise Exception("Function needs to be overridden in child class.")


class DataGTN(GTN):
    def compile(self,
                curriculum_loader: _typing.Collection[_torch.Tensor]
                ):
        """ Initializes parameters for Data GTN learning
        Parameters:
            data: PyTorch DataLoader (or iterable) containing synthetic data for Learner training
        Returns:
            None
        """
        #   teacher = _nn.DataParallel(teacher, device_ids=list(range(_torch.cuda.device_count())))
        self.params_to_train = []

        self.inner_loop_iterations = len(curriculum_loader)
        self.curriculum_loader = iter(_cycle(curriculum_loader))

        curriculum_data = []
        curriculum_labels = []

        for i in range(self.inner_loop_iterations):
            loader = next(self.curriculum_loader)
            curriculum_data += [loader[0]]
            curriculum_labels += [loader[1]]

        self.curriculum_data = _nn.Parameter(_torch.stack((curriculum_data), 0).detach(), requires_grad=True)
        self.curriculum_labels = _torch.stack(curriculum_labels, 0).detach()
        self.params_to_train += [self.curriculum_data]

    def get_innerloop_data(self, step) -> _typing.Tuple[_torch.Tensor, _torch.Tensor]:
        """ Called during training to feed data to Learner
        Parameters:
            step: current inner loop iteration number

        Returns:
           Data and labels for training in a list
        """

        return self.curriculum_data[step], self.curriculum_labels[step]


class CurriculumTeacherGTN(GTN):
    """ Implements a GTN with a Teacher model
    """
    def compile(self,
                teacher: _torch.nn.Module,
                teacher_noise: _torch.Tensor
                ) -> None:
        #   teacher = _nn.DataParallel(teacher, device_ids=list(range(_torch.cuda.device_count())))

        """ Prepares flow of data and optimizer for Teacher GTN learning
        Parameters:
            teacher: Teacher that takes in ``noise`` and outputs data that will be used by Learners
            teacher_noise: Noise to be used by the Teacher in the shape of ``(num_iterations, batch_size, teacher_input)``
        Returns:
            None
        """
        self.params_to_train = []

        self.teacher = teacher.to(self.device)
        self.params_to_train += list(self.teacher.parameters())
        self.teacher_noise = teacher_noise
        self.noise_size = self.teacher_noise.shape[2:]
        self.inner_batch_size = self.teacher_noise.shape[1]
        self.inner_loop_iterations = self.teacher_noise.shape[0]

        self.params_to_train += [self.teacher_noise]
        self.teacher_labels = _torch.arange(self.inner_batch_size) % self.num_classes
        self.one_hot = _nn.functional.one_hot(self.teacher_labels, self.num_classes)

    def get_innerloop_data(self, step) -> _typing.Tuple[_torch.Tensor, _torch.Tensor]:
        """ Called during training to feed data to Learner
        Parameters:
            step: current inner loop iteration number
        Returns:
           Data and labels for training in a list
        """
        z_vec = self.teacher_noise[step]
        return self.teacher(z_vec.to(self.device), self.one_hot.to(self.device)), self.teacher_labels


class RandomTeacherGTN(GTN):
    """ Implements a GTN with a Teacher model
    """
    def compile(self,
                teacher: _torch.nn.Module,
                inner_loop_iterations: int = 32,
                noise_size: _typing.List[int] = [128],
                inner_batch_size: int = 128,
                ) -> None:
        #   teacher = _nn.DataParallel(teacher, device_ids=list(range(_torch.cuda.device_count())))

        """ Prepares flow of data and optimizer for Teacher GTN learning
        Parameters:
            teacher: Teacher that takes in ``noise`` and outputs data that will be used by Learners
            inner_loop_iterations: Number of inner loop iterations
            noise_size: dim of noise that the ``teacher`` will accept
            teacher_noise: Directly provide teacher noise
        Returns:
            None
        """
        self.params_to_train = []

        self.inner_batch_size = inner_batch_size
        self.inner_loop_iterations = inner_loop_iterations
        self.noise_size = noise_size

        self.teacher = teacher.to(self.device)
        self.params_to_train += list(self.teacher.parameters())
        self.teacher_labels = _torch.arange(self.inner_batch_size) % self.num_classes
        self.one_hot = _nn.functional.one_hot(self.teacher_labels, self.num_classes)

    def get_innerloop_data(self, step) -> _typing.Tuple[_torch.Tensor, _torch.Tensor]:
        """ Called during training to feed data to Learner
        Parameters:
            step: current inner loop iteration number
        Returns:
           Data and labels for training in a list
        """

        z_vec = _torch.randn([self.inner_batch_size]+self.noise_size).to(self.device)
        return self.teacher(z_vec.to(self.device), self.one_hot.to(self.device)), self.teacher_labels
