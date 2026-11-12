FROM ubuntu:jammy

ARG DEBIAN_FRONTEND=noninteractive
SHELL ["/usr/bin/bash", "-c"]

# GitHub Actions runner 上 apt 偶发 DNS 解析失败（run 43059518498 实测
# "Could not resolve 'archive.ubuntu.com'"）。默认 apt-get update 对索引
# 全部下载失败只打印 W 警告并返回 0，链式命令会继续到 install，报出误导性的
# "no installation candidate" / "Unable to locate package sudo"。
# 故：严格模式让 update 真实失败 + 校验索引文件确实落盘 + 有限次退避重试。
# 不再强制 IPv4：IPv6 连接失败只是推断，实测是 DNS 解析失败；强制 IPv4
# 会单向收窄可用网络路径，去掉后正常路径不受影响（getent ahostsv4 已验证）。
# 该 apt 配置为全局配置，对下方大批量安装同样生效。
RUN printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::http::Timeout "60";' \
      'Acquire::https::Timeout "60";' \
      'APT::Update::Error-Mode "any";' \
      > /etc/apt/apt.conf.d/99rocknix-ci \
 && for i in 1 2 3; do \
      echo "apt-get update attempt ${i}/3"; \
      if apt-get update \
         && ls /var/lib/apt/lists/*Packages* >/dev/null 2>&1; then \
        break; \
      fi; \
      if [ "${i}" -eq 3 ]; then \
        echo "ERROR: apt indexes unavailable after 3 attempts" >&2; \
        getent hosts archive.ubuntu.com \
          || echo "DNS: cannot resolve archive.ubuntu.com" >&2; \
        exit 1; \
      fi; \
      echo "apt update failed, retrying in 10s" >&2; \
      sleep 10; \
    done \
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
