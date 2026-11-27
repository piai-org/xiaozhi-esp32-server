#!/usr/bin/env python3
"""Run synthetic backend regression tests without operator configuration or model calls."""
import importlib.util
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests/core/utils'))
sys.path.insert(0, str(ROOT / 'tests/providers'))
sys.dont_write_bytecode = True
os.chdir(ROOT)

with tempfile.TemporaryDirectory(prefix='xiaozhi-tests-') as work:
    source_root = ROOT
    ROOT = Path(work) / 'source'
    ROOT.mkdir()
    for name in ('core', 'config', 'plugins_func', 'tests'):
        shutil.copytree(source_root / name, ROOT / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('agent-base-prompt.txt', 'config.yaml'):
        shutil.copy2(source_root / name, ROOT / name)
    os.chdir(ROOT)
    sys.path[:0] = [str(ROOT), str(ROOT / 'tests/core/utils'), str(ROOT / 'tests/providers')]
    from core.utils.cache.manager import cache_manager, CacheType
    cache_manager.set(CacheType.CONFIG, 'main_config', {
        'log': {'log_level': 'ERROR', 'log_dir': work, 'data_dir': work}})
    from config import settings
    settings.config_file_valid = True
    from loguru import logger
    logger.remove()
    names = (
        'test_naming_context_repair', 'test_runtime_capabilities', 'test_prompt_template',
        'test_information_workflow', 'test_powermem_managed', 'test_memory_layers',
        'test_natural_response', 'test_recent_memory', 'test_delivery_context',
        'test_companion_response', 'test_companion_affect', 'test_companion_memory',
        'test_companion_topics', 'test_companion_speech', 'test_companion_initiative',
        'test_initiative_input_lifecycle', 'test_speech_plan', 'test_hybrid_reply',
        'test_aliyunbl_stream',
    )
    suite = unittest.TestSuite()
    for name in names:
        path = ROOT / ('tests/providers' if name == 'test_aliyunbl_stream' else 'tests/core/utils') / (name + '.py')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
