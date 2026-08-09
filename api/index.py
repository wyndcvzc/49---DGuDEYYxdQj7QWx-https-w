import os
import sys

# 确保项目根目录在 sys.path 中
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from mangum import Mangum
from main import app

# Vercel AWS Lambda handler
handler = Mangum(app, lifespan="auto")