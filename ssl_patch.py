"""
Patches pymongo's SSL to use TLS 1.2 and bypass certificate verification,
enabling connections through Accenture's TLS-inspection corporate proxy.

Root cause of TLSV1_ALERT_INTERNAL_ERROR: the corporate proxy does not support
TLS 1.3. Capping at TLS 1.2 lets the proxy complete the handshake.

Strategy: use sys.meta_path to intercept the import of pymongo.client_options
and wrap _parse_ssl_options, which is the function that creates and returns the
SSL context. Targeting this function (rather than pymongo.ssl_support.get_ssl_context)
is necessary because client_options uses a "from" import, giving it a direct
function reference that a module-level patch to ssl_support would not affect.

We do NOT replace ssl.SSLContext here, so there is no recursion risk and
ctx.maximum_version can be set with a plain Python assignment.

Must be imported before pymongo.
"""
import ssl as _ssl
import sys
import importlib.util
import importlib.abc


def _apply_permissive_settings(ctx):
    try:
        ctx.check_hostname = False
    except Exception:
        pass
    try:
        ctx.verify_mode = _ssl.CERT_NONE
    except Exception:
        pass
    try:
        ctx.maximum_version = _ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    try:
        ctx.set_ciphers('DEFAULT@SECLEVEL=0')
    except Exception:
        pass


class _PymongoPatcher(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """
    Intercepts import of pymongo.client_options and wraps _parse_ssl_options
    so that every SSL context pymongo creates gets TLS 1.2 enforced on it.
    """
    _real_loader = None

    def find_spec(self, fullname, path, target=None):
        if fullname != 'pymongo.client_options':
            return None
        # Remove ourselves before calling find_spec to prevent infinite re-entry.
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(fullname)
        if spec is None:
            return None
        self._real_loader = spec.loader
        spec.loader = self
        return spec

    def create_module(self, spec):
        if self._real_loader and hasattr(self._real_loader, 'create_module'):
            return self._real_loader.create_module(spec)
        return None

    def exec_module(self, module):
        # Let pymongo.client_options load normally first.
        if self._real_loader:
            self._real_loader.exec_module(module)

        orig = getattr(module, '_parse_ssl_options', None)
        if orig is None:
            return

        def _patched(*args, **kwargs):
            result = orig(*args, **kwargs)
            # result is (ssl_context, tls_allow_invalid_hostnames)
            ctx = result[0] if result else None
            if ctx is not None:
                _apply_permissive_settings(ctx)
            return result

        module._parse_ssl_options = _patched


sys.meta_path.insert(0, _PymongoPatcher())
