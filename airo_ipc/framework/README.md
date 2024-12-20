# Framework

The framework supplies a node class that can be used to create a process that communicates with other nodes using the
IPC framework. The node class provides a simple publish/subscribe system that abstracts away implementation details. The
node class is not required to use the IPC framework, but it can simplify some of your code.

## Features

- Easy-to-use interface for creating nodes that communicate using shared memory and DDS
- Simple publish/subscribe system for sending and receiving data between nodes
- Built-in support for shared memory and DDS communication

## Usage

See `examples/framework/` for examples of how to use the framework.
