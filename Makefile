.PHONY: install sample profile etl validate spec app test
install:   ; pip install -r requirements-dev.txt
sample:    ; python -m scripts.make_sample_data --out data/raw --years 2023 2024 --entidades 300
profile:   ; python -m etl.profile
etl:       ; python -m etl.run
validate:  ; python -m etl.validate
spec:      ; python -m scripts.check_spec --strict
app:       ; python -m dashboard.app
test:      ; python -m pytest
