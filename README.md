# airo-ipc

This repository contains utilities for inter-process communication (IPC) in Python, including shared memory
communication and DDS communication.
The DDS communication is based on the [Cyclone DDS framework](https://cyclonedds.io/) and the shared memory
communication is built on the shared memory primitives of Python 3.8+.

## Main features

- Fast shared memory communication using Python 3.8+ shared memory primitives
- DDS communication using the Cyclone DDS framework
- Easy-to-install with one `pip` command

The main benefit of using shared memory communication is that it is faster than other forms of IPC, such as sockets or
pipes, because it does not require copying data between processes. This is especially useful for high-throughput
applications, such as real-time video processing. By using CycloneDDS, we can also communicate between processes on
different machines, which is useful for distributed systems. Finally, this Python package is installable with a single
command, making it easy to use in any Python project, unlike other IPC libraries that require complex installation
procedures (including CycloneDDS with [Iceoryx](https://iceoryx.io/) for shared memory communication),
or [ROS 2](https://ros.org/).

Please refer to the README file in `airo_ipc/cyclone_shm` for more information on the shared memory communication.

## Installation

You can install the `airo-ipc` package by running:

```bash
pip install git+https://github.com/airo-ugent/airo-ipc
```

or by cloning the repository and running:

```bash
pip install -e airo-ipc/
```

## Usage

See [the shared memory communication README](airo_ipc/cyclone_shm/README.md) for more information on how to use the
shared memory communication utilities.

This repository also contains a `framework` package, which provides a high-level interface for using shared memory and
DDS communication together. It is not required, but may facilitate common use cases.
See [the framework README](airo_ipc/framework/README.md) for more information.

### Examples

See the `examples/` directory to learn how to use airo-ipc.
