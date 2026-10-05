# One consolidated container with EMANE built from source (Part 2 of the brief:
# "Install EMANE on a single Linux machine or via one consolidated Docker
# container").
#
# Build steps follow EMANE's own CI recipe (emane/ci/ubuntu/22.04/Dockerfile),
# pinned to the v1.5.3 release instead of the moving develop branch.
#
#   docker build -t vmn-emane emane/docker
#
# Run with --privileged: every emulated radio lives in its own network
# namespace and EMANE's virtual transport needs /dev/net/tun.

FROM ubuntu:22.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
# build-essential and autotools-dev are the names EMANE's debian/control
# Build-Depends expects. make deb runs dpkg-checkbuilddeps, which checks for
# build-essential by name, so installing gcc and make alone is not enough.
RUN apt-get -y update && apt-get -y install --no-install-recommends \
        ca-certificates git build-essential autoconf automake autotools-dev libtool \
        dh-python debhelper fakeroot \
        python3-setuptools libxml2-dev libprotobuf-dev libpcap-dev libpcre3-dev \
        uuid-dev pkg-config python3-protobuf protobuf-compiler
WORKDIR /opt
RUN git clone --depth 1 -b v1.5.3 https://github.com/adjacentlink/emane
WORKDIR /opt/emane
RUN ./autogen.sh && ./configure && make deb
RUN mkdir -p /opt/built && cp $(find .debbuild -name "*.deb") /opt/built

FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
COPY --from=build /opt/built /opt/built
RUN apt-get -y update && \
    (dpkg -i /opt/built/python3*.deb /opt/built/emane*.deb || true) && \
    apt-get -y install -f --no-install-recommends && \
    apt-get -y install --no-install-recommends \
        iproute2 iputils-ping procps python3 python3-lxml python3-networkx && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /work
CMD ["bash"]
