FROM ubuntu:jammy

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/usr/bin/bash", "-c"]

# apt 在 GitHub Actions runner 上偶发 IPv6 连接失败 / 下载超时，会让
# apt-get 以 exit 100 中止整个镜像构建。这里把重试、超时与强制 IPv4
# 写成 apt 全局配置，使本文件后续所有 apt 调用（含下方大批量安装）同样生效。
RUN printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::http::Timeout "60";' \
      'Acquire::https::Timeout "60";' \
      'Acquire::ForceIPv4 "true";' \
      > /etc/apt/apt.conf.d/99rocknix-ci \
 && apt-get update \
 && apt-get dist-upgrade -y \
 && apt-get install -y locales sudo

RUN locale-gen en_US.UTF-8 \
 && update-locale LANG=en_US.UTF-8 LANGUAGE=en_US:en
ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

RUN adduser --disabled-password --gecos '' docker \
 && adduser docker sudo \
 && echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

RUN apt-get install -y \
    bc cpio default-jre file gawk gcc git golang-go gperf libjson-perl libncurses5-dev \
    libparse-yapp-perl libxml-parser-perl lzop make patchutils python-is-python3  \
    python3 parted unzip wget curl xfonts-utils xsltproc zip xxd zstd rdfind automake \
    xmlstarlet rsync

### Cross compiling on ARM
RUN if [ "$(uname -m)" = "aarch64" ]; then apt-get install -y --no-install-recommends qemu-user-binfmt libc6-dev-amd64-cross; fi
RUN if [ ! -d /lib64 ]; then ln -sf /usr/x86_64-rocknix-linux-gnu/lib64 /lib64; fi
RUN if [ ! -d /lib/x86_64-rocknix-linux-gnu ]; then ln -sf /usr/x86_64-rocknix-linux-gnu/lib /lib/x86_64-rocknix-linux-gnu; fi

RUN mkdir -p /work && chown docker /work

WORKDIR /work
USER docker
