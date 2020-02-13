# PyTorch GTC Workshop Repo

This repo contains the PyTorch Distributed Deep Learning workshop contents to run on the Nvidia DLI platform. It will simulate two host environment with 2 GPUs per host.

# Docker Compose Instructions

## Requirements

For the purposes of the GTC demo, you will need
* `docker-ce 18.03`
* `docker-compose 1.24`
* NVIDIA driver 418.67 on host

## Usage

Use `docker-compose build` to build the services `node1` and `node2`.

Use `docker-compose up` to launch the services for development.

Use `docker-compose up -d` to launch the services as daemons, and `docker-compose down` to terminate the services.

# Docker Instructions

Use `Dockerfile` to build a docker image with `docker build --no-cache -t ptgtc .`.

Create private docker network for network connection across the hosts

`docker network create -d bridge --subnet 192.168.0.0/24 --gateway 192.168.0.1 backend`

Launch two docker containers (each simulating a host) using the docker image created.

Node 1:

`docker run -d --name node1 --network=backend  -p 8000:8888 --shm-size=16g -e NVIDIA_VISIBLE_DEVICES=0,1 --runtime=nvidia ptgtc`

Node 2: 

`docker run -d --name node2 --network=backend  -p 9000:8888 --shm-size=16g -e NVIDIA_VISIBLE_DEVICES=2,3 --runtime=nvidia ptgtc`

Once the containers are running, visit the content in your browser at `localhost:8000` and `localhost:9000`

Open a terminal window inside the juperlab browser window above and verify following commands are running.

`ping node2`

`lsof -i -P -n` to see the list of all the open ports

For testing the distributed data parallel across the two hosts, follow below sequence of steps:

1. On node1, open two terminal windows
2. From first terminal window run command `export NCCL_DEBUG=info`
3. From first terminal window run command `python ddp_tutorial.py 0 0`
4. From second terminal window run command `python ddp_tutorial.py 1 1`
5. On node2, open two terminal windows
6. From first terminal window run command `export NCCL_DEBUG=info`
7. From first terminal window run command `python ddp_tutorial.py 2 0`
8. From second terminal window run command `python ddp_tutorial.py 3 1`

The DDP training job should run and complete. 
