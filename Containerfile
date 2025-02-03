FROM python:3.9-slim

# Set the working directory
WORKDIR /app

COPY . /app

# Install dependencies based on pyproject.toml
RUN pip install --upgrade pip && pip install build
RUN python -m build
RUN pip install dist/*.whl  # or dist/*.tar.gz if it generates a source distribution

# Keep the container running
CMD ["sh", "-c", "abhier-indexer \"$INDEXER_SOURCE\" \"$INDEXER_DEST\" --schedule-hourly"]
