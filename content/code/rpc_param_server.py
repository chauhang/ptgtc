import argparse
import os
import time
from threading import Lock

import torch
import torch.distributed.autograd as dist_autograd
import torch.distributed.rpc as rpc
import torch.multiprocessing as mp
import torch.nn as nn
import torch.nn.functional as F
from torch import optim
from torch.distributed.optim import DistributedOptimizer
from torchvision import datasets, transforms

# --------- MNIST Network to train, from pytorch/examples -----


class Net(nn.Module):
    def __init__(self, num_gpus=0):
        super(Net, self).__init__()
        print("Net got ngpus {}".format(num_gpus))
        self.num_gpus = num_gpus
        device = torch.device(
            "cuda:0" if torch.cuda.is_available() and self.num_gpus > 0 else "cpu")
        print("Putting first 2 convs on {}".format(str(device)))
        # Put conv layers on the first cuda device
        self.conv1 = nn.Conv2d(1, 32, 3, 1).to(device)
        self.conv2 = nn.Conv2d(32, 64, 3, 1).to(device)
        # Put rest of the network on the 2nd cuda device, if there is one
        if "cuda" in str(device) and args.num_gpus > 1:
            device = torch.device("cuda:1")

        print("Putting rest of layers on {}".format(str(device)))
        self.dropout1 = nn.Dropout2d(0.25).to(device)
        self.dropout2 = nn.Dropout2d(0.5).to(device)
        self.fc1 = nn.Linear(9216, 128).to(device)
        self.fc2 = nn.Linear(128, 10).to(device)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.max_pool2d(x, 2)

        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        # need to put this on CUDA
        next_device = next(self.fc1.parameters()).device
        # print("In forward, changing device to {}".format(str(next_device)))
        x = x.to(next_device)

        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        output = F.log_softmax(x, dim=1)
        return output


# --------- Helper Methods --------------------

# On the local node, call a method with first arg as the value held by the
# RRef. Other args are passed in as arguments to the function called.
# Useful for calling instance methods.
def call_method(method, rref, *args, **kwargs):
    return method(rref.local_value(), *args, **kwargs)

# Synchronous RPC to run a method remotely and get a result.
# The method should be a class method corresponding to Given an RRef,
# return the result of calling the passed in method on the value
# held by the RRef. This call is done on the remote node that owns
# the RRef. args and kwargs are passed into the method.
# Example: If the value held by the RRef is of type Foo, then
# remote_method(Foo.bar, rref, arg1, arg2) is equivalent to calling
# <foo_instance>.bar(arg1, arg2) on the remote node and getting the result
# back.


def remote_method(method, rref, *args, **kwargs):
    args = [method, rref] + list(args)
    return rpc.rpc_sync(rref.owner(), call_method, args=args, kwargs=kwargs)


# --------- Parameter Server --------------------
class ParameterServer(nn.Module):
    def __init__(self, num_gpus=0):
        super().__init__()
        # TODO - configure with num_gpus.
        model = Net(num_gpus=num_gpus)
        self.model = model
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() and num_gpus > 0 else "cpu")
        # print("training on {}".format(str(self.device)))
        # self.model.to(self.device)

    def forward(self, inp):
        inp = inp.to(self.device)
        out = self.model(inp)
        return out

    # Use dist autograds to retrieve gradients accumulated for this model.
    # Primarily used for verification.
    def get_dist_gradients(self, cid):
        grads = dist_autograd.get_gradients(cid)
        return grads

    # Wrap local parameters in a RRef. Needed for building the
    # DistributedOptimizer which optimizes paramters remotely.
    def get_param_rrefs(self):
        param_rrefs = [rpc.RRef(param) for param in self.model.parameters()]
        return param_rrefs


param_server = None
global_lock = Lock()
# Ensure that we get only one handle to the ParameterServer.


def get_parameter_server(num_gpus=0):
    global param_server
    with global_lock:
        if not param_server:
            # construct it once
            param_server = ParameterServer(num_gpus=num_gpus)
            print(
                "Returning parameter server with ID {}".format(
                    id(param_server)))
        return param_server


def run_parameter_server(rank, world_size):
    # The parameter server just acts as a host for the model and responds to
    # requests from trainers, hence it does not need to run a loop.
    # rpc.shutdown() will wait for all workers to complete by default, which
    # in this case means that the parameter server will wait for all trainers
    # to complete, and then exit.
    rpc.init_rpc(name="parameter_server", rank=rank, world_size=world_size)
    print("RPC initialized! Running parameter server...")
    rpc.shutdown()
    print("RPC shutdown on parameter server.")


# --------- Trainers --------------------

# nn.Module corresponding to the network trained by this trainer. The
# forward() method simply invokes the network on the given parameter
# server.
class TrainerNet(nn.Module):
    def __init__(self, num_gpus=0):
        super().__init__()
        self.device = torch.device(
            "cuda:0" if torch.cuda.is_available() else "cpu")
        # TODO take this in as an arg
        self.param_server_rref = rpc.remote(
            "parameter_server", get_parameter_server, args=(num_gpus,))

    def get_global_param_rrefs(self):
        remote_params = remote_method(
            ParameterServer.get_param_rrefs,
            self.param_server_rref)
        return remote_params

    def forward(self, x, cid):
        model_output = remote_method(
            ParameterServer.forward, self.param_server_rref, x)
        return model_output


def run_training_loop(rank, num_gpus, train_loader, test_loader):
    # Runs the typical nueral network forward + backward + optimizer step, but
    # in a distributed fashion.
    net = TrainerNet(num_gpus=num_gpus)
    # Build DistributedOptmizer.
    param_rrefs = net.get_global_param_rrefs()
    opt = DistributedOptimizer(optim.SGD, param_rrefs, lr=0.03)
    for i, (data, target) in enumerate(train_loader):
        with dist_autograd.context() as cid:
            model_output = net(data, cid)
            print("Model output device {}".format(model_output.device))
            target = target.to(model_output.device)
            loss = F.nll_loss(model_output, target)
            if i % 5 == 0:
                print(
                    "Rank {} training batch {} loss {}".format(
                        rank, i, loss.item()))
            dist_autograd.backward([loss])
            # verify that we have remote gradients
            print(
                "Rank {} verifying gradients on master with cid {}".format(
                    rank, cid))
            assert remote_method(
                ParameterServer.get_dist_gradients,
                net.param_server_rref,
                cid) != {}
            opt.step()
            if i == 50:
                break  # break at 50 iters.

    print("Training complete!")
    print("Getting accuracy....")
    get_accuracy(test_loader, net)


def get_accuracy(test_loader, model):
    model.eval()
    correct_sum = 0
    with torch.no_grad():
        for data, target in test_loader:
            out = model(data, -1)
            pred = out.argmax(dim=1, keepdim=True)
            target = target.to(pred.device)
            correct = pred.eq(target.view_as(pred)).sum().item()
            correct_sum += correct
    print("Accuracy {}".format(correct_sum / len(test_loader.dataset)))


# Main loop for trainers.
def run_worker(rank, world_size, num_gpus, train_loader, test_loader):
    rpc.init_rpc(
        name="trainer_{}".format(rank),
        rank=rank,
        world_size=world_size)

    run_training_loop(rank, num_gpus, train_loader, test_loader)
    rpc.shutdown()

# --------- Launcher --------------------


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description="Parameter-Server RPC based training")
    parser.add_argument(
        "world_size",
        type=int,
        default=4,
        help="Total number of participating processes. Should be the sum of master node and all training nodes, add 1 if creating training node on master.")
    parser.add_argument(
        "rank",
        type=int,
        default=None,
        help="Global rank of this process. Pass in 0 for master. Note that ranks should be unique across all nodes participating in training.")
    parser.add_argument(
        "num_gpus",
        type=int,
        default=0,
        help="Number of GPUs to use for training, currently only supports 1 or 2.")
    parser.add_argument(
        "--master_addr",
        type=str,
        default="localhost",
        help="Address of master, will default to localhost if not provided. Master must be able to accept network traffic on the address + port.")
    parser.add_argument(
        "--master_port",
        type=str,
        default="29500",
        help="Port that master is listening on, will default to 29500 if not provided. Master must be able to accept network traffic on the host and port.")

    args = parser.parse_args()
    assert args.rank is not None, "must provide rank argument."
    os.environ['MASTER_ADDR'] = args.master_addr
    os.environ["MASTER_PORT"] = args.master_port
    # Get data to train on
    train_loader = torch.utils.data.DataLoader(
        datasets.MNIST('../data', train=True, download=True,
                       transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Normalize((0.1307,), (0.3081,))
                       ])),
        batch_size=32, shuffle=True,)
    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST('../data', train=False, transform=transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.1307,), (0.3081,))
        ])),
        batch_size=32, shuffle=True, )
    processes = []
    world_size = args.world_size
    if args.rank == 0:
        p = mp.Process(target=run_parameter_server, args=(0, world_size))
        p.start()
        processes.append(p)
    else:
        # start training worker on this node
        p = mp.Process(
            target=run_worker,
            args=(
                args.rank,
                world_size, args.num_gpus,
                train_loader,
                test_loader))
        p.start()
        processes.append(p)

    for p in processes:
        p.join()
