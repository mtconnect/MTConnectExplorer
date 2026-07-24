
all: bin

bin: build
#	Build the client wheel and install it over the editable install so the
#	frozen binary uses (and proves) the real wheel
	rm -rf lib/dist
	venv/bin/python -m build --wheel lib
	venv/bin/pip install --force-reinstall lib/dist/*.whl
	venv/bin/pyinstaller installer.spec --clean

build: src/bin/agent venv/touchfile

clean:
	rm -rf build/
	rm -rf datasets/
	rm -rf dist/
	rm -rf src/bin/
	rm -rf test/
	rm -rf venv/

# Update venv when pyproject.toml changes
venv/touchfile: requirements-src.txt
	echo "Setting up virtual environment..."
#	Create a virtual environment if it doesn't exist
	test -d venv || python3 -m venv venv
#	Install package with regular and dev dependencies from pyproject.toml
	venv/bin/pip install -Ur requirements-src.txt
#	Signal that venv has been updated due to pyproject.toml change
	touch venv/touchfile
#	Update requirements.txt (exclude the editable lib install - machine-specific path)
	venv/bin/pip freeze --exclude mtconnect-explorer-client > requirements.txt

src/bin/agent:
#	Create temporary build directory
	mkdir -p build
#	Download the .deb package
	cd build && \
		curl -s https://api.github.com/repos/mtconnect/cppagent/releases/latest \
		| grep "browser_download_url.*\.deb" \
		| cut -d '"' -f 4 \
		| xargs curl -LO
#	Extract deb contents
	cd build && ar x *.deb && tar -xf data.tar.gz
#	Create bin directory
	mkdir -p src/bin
#	Copy agent to bin
	cp build/usr/bin/agent src/bin/
#	Remove temporary build directory
	rm -rf build/
