"""
pdf_utils.py  —  iLovePDF-grade PDF compression before Dropbox upload.

Pipeline (best result wins, upload never fails):

  Stage 1 — Ghostscript /ebook preset          (binary, ~iLovePDF quality)
             150 DPI, JPEG-82, full resample.  Typical: 80 MB → 10–15 MB.

  Stage 2 — pikepdf + DPI-aware Pillow         (pure Python fallback)
             Decodes every XObject stream, downsamples if > 1600 px wide,
             re-encodes RGB/Gray as JPEG-68 and RGBA/P as PNG-9.
             Typical: 80 MB → 15–25 MB.

  Stage 3 — pypdf zlib                          (last-resort Python fallback)
             Recompresses content streams only.

  Stage 4 — passthrough                         (upload always succeeds)

The function never raises.  Only a stage result that is strictly smaller
than the current best (by ≥ 3 %) is accepted.
"""

import io
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_MIN_SAVING_RATIO = 0.97   # must be < 97 % of current best to be accepted
_MAX_IMAGE_DIM    = 1600   # px  — downsample if wider/taller than this
_JPEG_QUALITY     = 68     # iLovePDF “medium” equivalent
_GS_DPI           = 150    # Ghostscript output DPI


# ═════════════════════════════════════════════════════════════════════════
# Stage 1 — Ghostscript
# ═════════════════════════════════════════════════════════════════════════

def _find_ghostscript() -> str | None:
    """Return the gs executable path, or None if not found."""
    for candidate in ('gs', 'gswin64c', 'gswin32c'):
        try:
            result = subprocess.run(
                [candidate, '--version'],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return candidate
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _compress_ghostscript(data: bytes, dpi: int = _GS_DPI) -> bytes:
    """
    Run Ghostscript with the /ebook preset.

    /ebook  — 150 DPI colour/gray images, JPEG quality ~82, colour-managed.
               Equivalent to iLovePDF “medium quality”.
    """
    gs = _find_ghostscript()
    if gs is None:
        raise RuntimeError('Ghostscript (gs) not found on PATH')

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as fin:
        fin.write(data)
        fin_path = fin.name

    fout_path = fin_path + '_compressed.pdf'
    try:
        cmd = [
            gs,
            '-q',                          # quiet
            '-dNOPAUSE',
            '-dBATCH',
            '-dSAFER',
            '-sDEVICE=pdfwrite',
            '-dCompatibilityLevel=1.4',
            '-dPDFSETTINGS=/ebook',        # 150 DPI, JPEG-82
            '-dEmbedAllFonts=true',
            '-dSubsetFonts=true',
            '-dAutoRotatePages=/None',
            '-dColorImageDownsampleType=/Bicubic',
            f'-dColorImageResolution={dpi}',
            '-dGrayImageDownsampleType=/Bicubic',
            f'-dGrayImageResolution={dpi}',
            '-dMonoImageDownsampleType=/Bicubic',
            f'-dMonoImageResolution={dpi}',
            '-dColorImageDownsampleThreshold=1.0',
            '-dGrayImageDownsampleThreshold=1.0',
            '-dMonoImageDownsampleThreshold=1.0',
            '-dOptimize=true',
            '-dFastWebView=true',
            '-sOutputFile=' + fout_path,
            fin_path,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=300,   # 5-minute timeout for large PDFs
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors='replace')[:400]
            raise RuntimeError(f'gs exited {proc.returncode}: {err}')

        with open(fout_path, 'rb') as f:
            return f.read()
    finally:
        for p in (fin_path, fout_path):
            try:
                os.unlink(p)
            except OSError:
                pass


# ═════════════════════════════════════════════════════════════════════════
# Stage 2 — pikepdf + DPI-aware Pillow  (pure Python, no binary)
# ═════════════════════════════════════════════════════════════════════════

def _recompress_xobject(pdf_obj, pikepdf_module, max_dim: int, jpeg_quality: int):
    """
    Re-encode a single image XObject in-place.

    Key fix over the previous version:
      • Uses pikepdf’s built-in stream decoding (read_raw_bytes=False) so
        CCITTFax, JBIG2, and LZW images are transparently decoded before
        Pillow sees them.
      • DPI-aware downsampling: if the image is wider/taller than max_dim,
        it is scaled down with LANCZOS before re-encoding.
      • Correct obj.write() call with the right filter argument.
    """
    from PIL import Image
    pikepdf = pikepdf_module

    try:
        # Let pikepdf decode the stream (handles all filter types)
        raw_decoded = pdf_obj.read_bytes()
        w = int(pdf_obj['/Width'])
        h = int(pdf_obj['/Height'])
        bpc = int(pdf_obj.get('/BitsPerComponent', 8))
        cs  = str(pdf_obj.get('/ColorSpace', '/DeviceRGB'))

        if '/DeviceGray' in cs or 'Gray' in cs:
            pil_mode = 'L'
        elif '/DeviceCMYK' in cs or 'CMYK' in cs:
            pil_mode = 'CMYK'
        else:
            pil_mode = 'RGB'

        try:
            img = Image.frombytes(pil_mode, (w, h), raw_decoded)
        except Exception:
            # Fallback: let Pillow sniff the format (works for inline JPEG)
            img = Image.open(io.BytesIO(raw_decoded))

    except Exception as exc:
        logger.debug('pdf_utils: decode failed for xobj: %s', exc)
        return

    # Downsample if image is excessively large (e.g. 600 DPI scan)
    if img.width > max_dim or img.height > max_dim:
        ratio   = min(max_dim / img.width, max_dim / img.height)
        new_w   = max(1, int(img.width  * ratio))
        new_h   = max(1, int(img.height * ratio))
        img     = img.resize((new_w, new_h), Image.LANCZOS)

    buf  = io.BytesIO()
    mode = img.mode

    try:
        if mode == 'CMYK':
            img = img.convert('RGB')
            mode = 'RGB'

        if mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            img.save(buf, format='PNG', optimize=True, compress_level=9)
            buf.seek(0)
            raw_out = buf.read()
            new_filter = pikepdf.Name('/FlateDecode')
            new_cs     = '/DeviceRGB'
            new_bpc    = 8
            new_w, new_h = img.width, img.height
        else:
            if mode not in ('RGB', 'L'):
                img  = img.convert('RGB')
                mode = 'RGB'
            img.save(
                buf, format='JPEG',
                quality=jpeg_quality,
                optimize=True,
                subsampling=2,    # 4:2:0 chrominance subsampling
                progressive=True,
            )
            buf.seek(0)
            raw_out    = buf.read()
            new_filter = pikepdf.Name('/DCTDecode')
            new_cs     = '/DeviceGray' if mode == 'L' else '/DeviceRGB'
            new_bpc    = 8
            new_w, new_h = img.width, img.height

        # Only replace if the re-encoded image is actually smaller
        if len(raw_out) >= len(raw_decoded):
            return

        pdf_obj.write(raw_out, filter=new_filter)
        pdf_obj['/Width']            = new_w
        pdf_obj['/Height']           = new_h
        pdf_obj['/ColorSpace']       = pikepdf.Name(new_cs)
        pdf_obj['/BitsPerComponent'] = new_bpc

        for stale in ('/DecodeParms', '/SMask', '/Mask', '/Intent',
                      '/Alternates', '/OPI', '/Metadata'):
            try:
                del pdf_obj[stale]
            except Exception:
                pass

    except Exception as exc:
        logger.debug('pdf_utils: encode failed for xobj: %s', exc)


def _walk_resources(page_or_form, pikepdf_module, max_dim, jpeg_quality, _visited=None):
    """
    Recursively walk XObjects (including Form XObjects) and recompress images.
    """
    if _visited is None:
        _visited = set()

    try:
        resources = page_or_form.get('/Resources', {})
        xobjects  = resources.get('/XObject', {})
    except Exception:
        return

    for key in list(xobjects.keys()):
        try:
            obj = xobjects[key]
            obj_id = obj.objgen

            if obj_id in _visited:
                continue
            _visited.add(obj_id)

            subtype = str(obj.get('/Subtype', ''))

            if subtype == '/Image':
                _recompress_xobject(obj, pikepdf_module, max_dim, jpeg_quality)

            elif subtype == '/Form':
                # Form XObjects can contain nested images
                _walk_resources(obj, pikepdf_module, max_dim, jpeg_quality, _visited)

        except Exception as exc:
            logger.debug('pdf_utils: skipped xobj %s: %s', key, exc)


def _compress_pikepdf_with_images(
    data: bytes,
    jpeg_quality: int = _JPEG_QUALITY,
    max_dim:      int = _MAX_IMAGE_DIM,
) -> bytes:
    """Full pikepdf + Pillow compression pass."""
    import pikepdf

    with pikepdf.open(io.BytesIO(data)) as pdf:
        pdf.remove_unreferenced_resources()

        visited = set()
        for page in pdf.pages:
            _walk_resources(page.obj, pikepdf, max_dim, jpeg_quality, visited)

        out = io.BytesIO()
        pdf.save(
            out,
            compress_streams=True,
            stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
            recompress_flate=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            normalise_content=True,
            linearize=True,    # Fast Web View
        )
        return out.getvalue()


# ═════════════════════════════════════════════════════════════════════════
# Stage 3 — pypdf zlib fallback
# ═════════════════════════════════════════════════════════════════════════

def _compress_with_pypdf(data: bytes) -> bytes:
    """Rewrite PDF with max zlib on content streams (no image re-encoding)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()

    for page in reader.pages:
        writer.add_page(page)

    for page in writer.pages:
        try:
            page.compress_content_streams(level=9)
        except TypeError:
            page.compress_content_streams()

    try:
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    except AttributeError:
        pass

    if reader.metadata:
        writer.add_metadata(reader.metadata)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ═════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════

def compress_pdf(file_obj) -> tuple:
    """
    Read *file_obj* (Django UploadedFile or any file-like with .read()),
    run through the compression pipeline, and return:

        (best_bytes, original_size_bytes, best_size_bytes, method_label)

    Never raises.  Falls back to passthrough on all failures.
    """
    file_obj.seek(0)
    original_data = file_obj.read()
    original_size = len(original_data)
    best_data     = original_data
    best_size     = original_size
    best_method   = 'passthrough'

    def _accept(new_data: bytes, label: str) -> bool:
        nonlocal best_data, best_size, best_method
        if new_data and len(new_data) < best_size * _MIN_SAVING_RATIO:
            best_data   = new_data
            best_size   = len(new_data)
            best_method = label
            logger.info('pdf_utils: %s accepted — %.1f MB (%.0f%% of original)',
                        label, best_size / 1_048_576,
                        100 * best_size / original_size)
            return True
        return False

    # ── Stage 1: Ghostscript /ebook ───────────────────────────────────────────
    try:
        _accept(_compress_ghostscript(original_data), 'ghostscript')
    except RuntimeError as exc:
        # gs not installed — expected on some hosts, not an error
        logger.info('pdf_utils: stage1 skipped: %s', exc)
    except Exception as exc:
        logger.warning('pdf_utils: stage1 failed: %s', exc)

    # ── Stage 2: pikepdf + Pillow DPI-aware ─────────────────────────────────
    try:
        compressed = _compress_pikepdf_with_images(original_data)
        _accept(compressed, 'pikepdf+pillow')
    except ImportError as exc:
        logger.info('pdf_utils: stage2 skipped (missing: %s)', exc)
    except Exception as exc:
        logger.warning('pdf_utils: stage2 failed: %s', exc)

    # ── Stage 3: pypdf zlib ───────────────────────────────────────────────────
    try:
        compressed = _compress_with_pypdf(original_data)
        _accept(compressed, 'pypdf')
    except ImportError:
        logger.info('pdf_utils: pypdf not installed')
    except Exception as exc:
        logger.warning('pdf_utils: stage3 failed: %s', exc)

    return best_data, original_size, best_size, best_method


def human_size(n_bytes: int) -> str:
    """Return a human-readable file size string, e.g. ‘45.2\u00a0MB’."""
    n = float(n_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f}\u00a0{unit}'
        n /= 1024
    return f'{n:.1f}\u00a0TB'
