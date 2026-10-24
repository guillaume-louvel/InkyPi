import os
import sys

# The app imports its modules relative to src/ (e.g. `from utils.app_utils import ...`)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
