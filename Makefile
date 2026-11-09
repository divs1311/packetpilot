.PHONY: install test bench api ui worker seed

install:
	pip install -r requirements.txt && pip install --no-deps -e .
test:
	pytest
bench:
	python -m packetpilot.bench.generate --out benchmark/packages --n 48 --seed 7
	python -m packetpilot.bench.evaluate --packages benchmark/packages --out benchmark/results
api:
	uvicorn packetpilot.api.main:app --reload
worker:
	python -m packetpilot.worker
ui:
	streamlit run streamlit_app.py
seed:
	python scripts/seed_demo.py --n 12
