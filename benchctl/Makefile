SCENARIO ?= scenarios/counter-rate.yaml

run:
	python3 driver/run.py $(SCENARIO)
	python3 verify/check.py

smoke:
	@echo "smoke = unit checks that need no containers"
	python3 -m py_compile driver/run.py verify/check.py modules/prometheus/init.py
	python3 -c "import yaml,json; s=yaml.safe_load(open('scenarios/counter-rate.yaml')); assert s['systems']==['prometheus']; print('scenario OK:', s['id'])"
	python3 -c "import yaml; m=yaml.safe_load(open('modules/prometheus/module.yaml')); assert m['image'].startswith('prom/prometheus'); print('module OK:', m['name'])"
