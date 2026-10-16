import ast
import json
import pickle
from pathlib import Path

import pytest
from cattrs.preconf.json import make_converter

from requests_cache import CachedResponse
from requests_cache.serializers import CattrStage, pickle_serializer, ujson_serializer


@pytest.fixture
def benchmark():
    path = Path(__file__).parents[1] / 'benchmark_serializers.py'
    source = ast.parse(path.read_text(), filename=str(path))
    functions = ast.Module(
        body=[node for node in source.body if isinstance(node, ast.FunctionDef)], type_ignores=[]
    )
    output = []
    clock = iter([0.0, 0.25, 0.5, 1.0])
    scope = {
        'CattrStage': CattrStage,
        'make_converter': make_converter,
        'ujson_serializer': ujson_serializer,
        'ITERATIONS': 2,
        'r': CachedResponse(content=b'synthetic response'),
        'time': lambda: next(clock),
        'print': output.append,
    }
    # Execute the real functions without the benchmark's live HTTP setup.
    exec(compile(functions, str(path), 'exec'), scope)
    return scope, output


@pytest.mark.parametrize(
    'name, serializer',
    [('pickle', pickle), ('cattrs', CattrStage()), ('cattrs+pickle', pickle_serializer)],
)
def test_benchmark_labels_operations_without_requiring_serializer_name(benchmark, name, serializer):
    scope, output = benchmark

    scope['run_serialize_deserialize'](name, serializer)

    assert output == [f'{name}.dumps() x2: 0.250', f'{name}.loads() x2: 0.500']


def test_ujson_benchmark_uses_complete_serializer(benchmark):
    pytest.importorskip('ujson')
    scope, _ = benchmark
    calls = []
    scope['run_serialize_deserialize'] = lambda name, serializer: calls.append((name, serializer))

    scope['run_cattrs_ujson']()

    assert calls == [('cattrs+ujson', ujson_serializer)]
    serialized = ujson_serializer.dumps(scope['r'])
    assert isinstance(json.loads(serialized), dict)
    assert ujson_serializer.loads(serialized).content == scope['r'].content
