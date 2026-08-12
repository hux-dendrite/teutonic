"""MiMo V2 architecture marker.

MiMo V2.5 Pro checkpoints carry their own pinned Transformers config/model
modules. The eval server loads those modules from the immutable snapshot with
``trust_remote_code=True`` after the validator verifies their names and hashes.
Unlike built-in architectures, there is therefore no global Auto* registration
to perform when this package is imported.
"""

MODEL_TYPE = "mimo_v2"
ALLOWED_CODE_FILES = ("configuration_mimo_v2.py", "modeling_mimo_v2.py")

__all__ = ["ALLOWED_CODE_FILES", "MODEL_TYPE"]
