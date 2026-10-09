#!/bin/bash

docker build \
  --platform linux/arm64 \
  -t music-bar-detector .
