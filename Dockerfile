FROM rapidsai/rapidsai:0.10-cuda9.2-runtime-ubuntu16.04
  
# CuPy is used for GPU-Accelerated UDFs
RUN conda run -n rapids pip install cupy-cuda92

# Update to latest jupyterlab and rebuild modules
RUN conda run -n rapids pip install --upgrade jupyterlab
RUN conda run -n rapids jupyter lab build


#Add pytorch
RUN conda run -n rapids pip install torch torchvision
RUN conda run -n rapids pip install classy_vision captum ax-platform

RUN  apt-get  update -y \
&& apt-get upgrade -y \
&& apt-get install iputils-ping -y \
&& apt-get install net-tools -y \
&& apt-get install -y lsof 

# Create working directory to add repo.
WORKDIR /workshop

# Load contents into student working directory.
ADD . .

# Create working directory for students.
WORKDIR /workshop/content

# Jupyter listens on 8888.
EXPOSE 8888

# Please see `entrypoint.sh` for details on how this content
# is launched.
ADD entrypoint.sh /usr/local/bin
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
