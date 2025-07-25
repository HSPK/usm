#!/bin/bash

sudo apt --purge remove "*nvidia*"
sudo apt-get install nvidia-driver-535 -y
sudo apt install cuda-toolkit-12-2 -y
sudo apt-get install libvulkan1 -y
sudo apt install vulkan-tools libvulkan-dev vulkan-validationlayers-dev spirv-tools -y
