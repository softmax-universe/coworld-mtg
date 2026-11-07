FROM ghcr.io/softmax-universe/commissioners-default:latest

COPY commissioner/coworld-mtg.yaml /config/coworld-mtg.yaml
ENV RULESET_STRATEGY_CONFIG_PATH=/config/coworld-mtg.yaml
