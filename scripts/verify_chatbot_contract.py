#!/usr/bin/env python3
from app.services.generation_contract import MODEL_ID, MODEL_VARIANT, REGION, SYSTEM_SHA256

assert MODEL_ID == "deepseek.v3.2"
assert MODEL_VARIANT == "hardened_analyst_scope_v4"
assert REGION == "eu-north-1"
assert SYSTEM_SHA256 == "2c2e85546f8d1c66330f393c691abf50dfa233cfcd5546e520c2d66e8b78f050"
print("PASS: exact hardened-v4 generation identity")
print("PASS: Bedrock region explicitly pinned to eu-north-1")
