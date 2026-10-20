SCENARIO ?= scenarios/prom-vs-pg-load.yaml

run:
	export BENCHCTL_SCENARIO=$(SCENARIO); \
	python3 driver/run.py $(SCENARIO); \
	python3 verify/check.py

# Filesystem loadtest: one database, one dataset, five filesystems. Each
# leg formats a real filesystem on a loop device and bind-mounts it into the
# container's data directory. fs-norm is the same matrix for the other
# database layout; it is a separate scenario because the filesystems inside
# one scenario have to share a disk to be comparable.
fs:
	export BENCHCTL_SCENARIO=scenarios/fs.yaml; \
	python3 driver/run.py scenarios/fs.yaml; \
	python3 verify/check.py

fs-norm:
	export BENCHCTL_SCENARIO=scenarios/fs-norm.yaml; \
	python3 driver/run.py scenarios/fs-norm.yaml; \
	python3 verify/check.py

down:
	docker compose -f monitoring/compose.monitoring.yml down -v 2>/dev/null || docker-compose -f monitoring/compose.monitoring.yml down -v 2>/dev/null || true
	docker network rm benchctl-mon 2>/dev/null || true
	@echo "note: filesystems mounted by a crashed run stay mounted."
	@echo "  sudo umount /mnt/benchctl-fs/mnt/* 2>/dev/null || true"
	@echo "  sudo losetup -a | grep benchctl-fs | cut -d: -f1 | xargs -r sudo losetup -d"

smoke:
	@echo "smoke = unit checks that need no containers"
	python3 -m py_compile driver/run.py verify/check.py modules/prometheus/init.py modules/postgres/observe.py shared/fssetup.py shared/leg.py
	python3 -c "import yaml,json; s=yaml.safe_load(open('scenarios/prom-vs-pg-load.yaml')); assert s['systems']==['prometheus','postgres/jsonb','postgres/norm']; print('scenario OK:', s['id'])"
	python3 -c "import yaml; m=yaml.safe_load(open('modules/prometheus/module.yaml')); assert m['image'].startswith('prom/prometheus'); print('module OK:', m['name'])"
	python3 -c "import yaml; s=yaml.safe_load(open('scenarios/fs.yaml')); assert s['filesystems']==['ext4','xfs','btrfs','zfs','tmpfs']; print('fs scenario OK:', s['id'])"
	python3 -c "import yaml; s=yaml.safe_load(open('scenarios/fs-norm.yaml')); assert s['systems']==['postgres/norm']; print('fs-norm scenario OK:', s['id'])"
	python3 -c "import sys; sys.path.insert(0,'shared'); from leg import leg_slug; assert leg_slug('postgres/jsonb','xfs')=='postgres-jsonb-xfs'; print('slug rule OK')"
