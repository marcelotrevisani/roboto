FROM condaforge/miniforge3:latest

WORKDIR /code

COPY ./environment.yaml /code/environment.yaml

RUN conda init
RUN conda env create -n roboto-env -f /code/environment.yaml
