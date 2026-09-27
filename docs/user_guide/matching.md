(matching)=
# {fas}`equals` Request Matching
Requests are matched according to the request method, URL, parameters and body. All of these values
are normalized to account for any variations that do not modify response content.

There are some additional options to configure how you want requests to be matched.

(filter-params)=
## Selective Parameter Matching
By default, all normalized request parameters are matched. In some cases, there may be request
parameters that you don't want to match. For example, an authentication token will change frequently
but not change response content.

Use the `ignored_parameters` option if you want to ignore specific parameters.

```{note}
Many common authentication parameters are already ignored by default.
See {ref}`default-filter-params` for details.
```

**Request Parameters:**

In this example, only the first request will be sent, and the second request will be a cache hit
due to the ignored parameters:
```python
>>> session = CachedSession(ignored_parameters=['auth-token'])
>>> session.get('https://httpbin.org/get', params={'auth-token': '2F63E5DF4F44'})
>>> r = session.get('https://httpbin.org/get', params={'auth-token': 'D9FAEB3449D3'})
>>> assert r.from_cache is True
```

**Request Body Parameters:**

This also applies to parameters in a JSON-formatted request body:
```python
>>> session = CachedSession(allowable_methods=('GET', 'POST'), ignored_parameters=['auth-token'])
>>> session.post('https://httpbin.org/post', json={'auth-token': '2F63E5DF4F44'})
>>> r = session.post('https://httpbin.org/post', json={'auth-token': 'D9FAEB3449D3'})
>>> assert r.from_cache is True
```

**Request Headers:**

As well as headers, if `match_headers=True` is used:
```python
>>> session = CachedSession(ignored_parameters=['auth-token'], match_headers=True)
>>> session.get('https://httpbin.org/get', headers={'auth-token': '2F63E5DF4F44'})
>>> r = session.get('https://httpbin.org/get', headers={'auth-token': 'D9FAEB3449D3'})
>>> assert r.from_cache is True
```
```{note}
Since `ignored_parameters` is most often used for sensitive info like credentials, these values will also be removed from the cached request parameters, body, and headers.
Ignoring `Cookie` also removes stored request cookie jars without changing the live request.
Redaction includes stored redirect requests and prepared next requests.
Existing cache files are not scrubbed automatically.
```

```{tip}
Variations in headers can be introduced by the libraries used by CacheSession.
These variations can be eliminated by calling CachedSession request functions
with a fixed header value. For example, always passing `"Accept-Encoding": "gzip,
deflate"` ensures that additional compression encodings are not added to the request.
```

(matching-headers)=
## Matching Request Headers
```{note}
In some cases, request header values can affect response content. For example, sites that support
i18n and [content negotiation](https://developer.mozilla.org/en-US/docs/Web/HTTP/Content_negotiation) may use the `Accept-Language` header to determine which language to serve content in.

The server will ideally also send a `Vary` header in the response, which informs caches about
which request headers to match. Requests-cache checks those headers before reusing a response.
Different values of headers such as `Accept` can be cached as separate variants.
A `Vary` field containing `*` always prevents reuse, including when it lists other headers.
Not all servers send `Vary`, however.
```

Use the `match_headers` option if you want to specify which headers you want to match when `Vary`
isn't available:
```python
>>> session = CachedSession(match_headers=['Accept'])
>>> # These two requests will be sent and cached separately
>>> session.get('https://httpbin.org/headers', headers={'Accept': 'text/plain'})
>>> session.get('https://httpbin.org/headers', headers={'Accept': 'application/json'})
```

If you want to match _all_ request headers, you can use `match_headers=True`.
Header names are case-insensitive. Header values retain their case, whitespace and order,
because their meaning depends on the server and header field.
Byte-valued headers use the same Latin-1 mapping as Requests, including with JSON serialisers.
The literal bytes `b'@@@SKIP_HEADER@@@'` remain distinct from urllib3's string control value.
Clear older entries containing non-ASCII byte headers or that literal byte value, since their
original bytes and types cannot always be recovered. New JSON entries use a tagged value for
that byte literal; clear those entries before returning to an older version.
Keys using header matching also change; clear the cache or use `session.cache.recreate_keys()`.
`Vary: Cookie` compares the exact sent header, including an explicit header that overrides a cookie jar.
Use `match_headers=['Cookie']` to retain different Cookie variants separately.
For responses with redirect history and `Vary: Cookie`, only a request matching the final method,
normalised URL and body can reuse the cached response directly. Requests through other redirect
aliases must follow the redirects again; cache-only requests return 504.
The final identity must still be available. Redacted values, streamed bodies and JSON-array bodies
cannot establish that match; arrays may have lost ignored values without a redaction marker.
If a nominated value or the `Vary` field itself was redacted, the cached response cannot be reused.


(custom-matching)=
## Custom Request Matching
If you need more advanced behavior, you can implement your own custom request matching.

### Cache Keys
Request matching is accomplished using a **cache key**, which uniquely identifies a response in the
cache based on request info. For example, the option `ignored_parameters=['foo']` works by excluding
the value of the `foo` request parameter from the cache key. Its presence still matters, so only
the second and third requests below share a cached response:
```python
>>> session = CachedSession(ignored_parameters=['foo'])
>>> response_1 = session.get('https://example.com')          # cache miss
>>> response_2 = session.get('https://example.com?foo=bar')  # cache miss
>>> response_3 = session.get('https://example.com?foo=qux')  # cache hit
>>> assert response_2.cache_key == response_3.cache_key
```

### Recreating Cache Keys
There are some situations where request matching behavior may change, which causes previously cached
responses to become obsolete:
* You start using a custom cache key, or change other settings that affect request matching
* A new version of requests-cache is released that includes new or changed request matching behavior
  (typically, most non-patch releases)

In these cases, if you want to keep using your existing cache data, you can use the
`recreate_keys` method:
```python
>>> session = CachedSession()
>>> session.cache.recreate_keys()
```

### Cache Key Functions
If you want to implement your own request matching, you can provide a cache key function which will
take a {py:class}`~requests.PreparedRequest` plus optional keyword args for
{py:func}`~requests.request`, and return a string:
```python
def create_key(request: requests.PreparedRequest, **kwargs) -> str:
    """Generate a custom cache key for the given request"""
```

You can then pass this function via the `key_fn` param:
```python
session = CachedSession(key_fn=create_key)
```

`**kwargs` includes relevant {py:class}`.BaseCache` settings and any other keyword args passed to
{py:meth}`.CachedSession.send`. If you want use a custom matching function _and_ the existing
options `ignored_parameters` and `match_headers`, you can implement them in `key_fn`:
```python
def create_key(
    request: requests.PreparedRequest,
    ignored_parameters: list[str] = None,
    match_headers: list[str] = None,
    **kwargs,
) -> str:
    """Generate a custom cache key for the given request"""
```

Reference:
* See {py:func}`.create_key` for the reference implementation.
* See the rest of the {py:mod}`.cache_keys` module for some useful helper functions.
* See {ref}`Examples<custom_keys>` for a complete example of custom request matching.


```{tip}
As a general rule, if you include less information in your cache keys, you will have more cache hits
and use less storage space, but risk getting incorrect response data back.
```
```{warning}
If you provide a custom key function for a non-empty cache, any responses previously cached with a
different key function will be unused, so it's recommended to clear the cache first.
```

### Custom Header Normalization
When matching request headers (using `match_headers` or `Vary`), requests-cache will normalize minor
header variations like order, casing, whitespace, etc. In some cases, you may be able to further
optimize your requests with some additional header normalization.

For example, let's say you're working with a site that supports content negotiation using the
`Accept-Encoding` header, and the only variation you care about is whether you requested gzip
encoding. This example will increase cache hits by ignoring variations you don't care about:
```python
from requests import PreparedRequest
from requests_cache import CachedSession, create_key


def create_custom_key(request: PreparedRequest, **kwargs) -> str:
    # Don't modify the original request that's about to be sent
    request = request.copy()

    # Simplify values like `Accept-Encoding: gzip, compress, br` to just `Accept-Encoding: gzip`
    if 'gzip' in request.headers.get('Accept-Encoding', ''):
        request.headers['Accept-Encoding'] = 'gzip'
    else:
        request.headers['Accept-Encoding'] = None

    # Use the default key function to do the rest of the work
    return create_key(request, **kwargs)


# Provide your custom request matcher when creating the session
session = CachedSession(key_fn=create_custom_key)
```
