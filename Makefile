.PHONY: test lint install dev-install

test:
	python3 -m pytest tests/ -v

lint:
	find lib/ai_monitor -name '*.py' -print0 | xargs -0 python3 -m py_compile

# --break-system-packages is needed for Homebrew's Python 3 (PEP 668);
# harmless on other Pythons. Combined with --user it installs to the user
# site-packages, not the system one.
dev-install:
	python3 -m pip install --user --break-system-packages -e .

install:
	python3 -m pip install --user --break-system-packages .
