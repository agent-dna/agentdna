import sys
from pathlib import Path

# Let the tests import the example's modules (research_tools, graph, ...).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
