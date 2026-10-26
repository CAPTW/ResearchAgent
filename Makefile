TOP ?= 3
WEEKLY_TOP ?= 15
RANK ?= 1

.PHONY: all
all:
\tpython3 tools/pipeline.py --top $(TOP) --weekly-top $(WEEKLY_TOP) --experiment --rank $(RANK)
