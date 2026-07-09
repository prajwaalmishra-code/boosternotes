import os
import time
import dropbox
from django.conf import settings
from dropbox import Dropbox, exceptions
from dropbox.exceptions import ApiError


class DropboxManager:
    """Handle all Dropbox operations."""

    @staticmethod
    def get_dropbox_client():
        return Dropbox(
            oauth2_refresh_token=settings.DROPBOX_REFRESH_TOKEN,
            app_key=settings.DROPBOX_APP_KEY,
            app_secret=settings.DROPBOX_APP_SECRET,
        )

    # ── helpers ────────────────────────────────────────────────────────────────
    @staticmethod
    def _read_chunk(file_obj, size):
        """Read exactly `size` bytes (or fewer at EOF)."""
        return file_obj.read(size)

    @staticmethod
    def _upload_with_retry(fn, *args, retries=3, backoff=2, **kwargs):
        """Call `fn(*args, **kwargs)` up to `retries` times on transient errors."""
        last_exc = None
        for attempt in range(retries):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
        raise last_exc

    # ── public API ─────────────────────────────────────────────────────────────
    @staticmethod
    def upload_file(file_obj, file_name, folder_path=None):
        """
        Upload a file to Dropbox using simple upload for files <= 4 MB,
        and upload sessions (chunked) for larger files.

        Returns a dict with keys: success, dropbox_path, link, message / error.
        """
        try:
            dbx = DropboxManager.get_dropbox_client()

            if folder_path is None:
                folder_path = settings.DROPBOX_FOLDER

            folder_path = str(folder_path).strip("/")
            file_name   = os.path.basename(file_name)
            full_path   = f"/{folder_path}/{file_name}"

            file_obj.seek(0)
            file_size  = file_obj.size          # InMemoryUploadedFile / TemporaryUploadedFile
            CHUNK_SIZE = 4 * 1024 * 1024        # 4 MB

            if file_size <= CHUNK_SIZE:
                # ── simple upload ─────────────────────────────────────────────
                DropboxManager._upload_with_retry(
                    dbx.files_upload,
                    file_obj.read(),
                    full_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )
            else:
                # ── chunked upload session ────────────────────────────────────
                # 1. Start session with the first chunk
                first_chunk = DropboxManager._read_chunk(file_obj, CHUNK_SIZE)
                session = DropboxManager._upload_with_retry(
                    dbx.files_upload_session_start, first_chunk
                )
                offset = len(first_chunk)

                cursor = dropbox.files.UploadSessionCursor(
                    session_id=session.session_id,
                    offset=offset,
                )
                commit = dropbox.files.CommitInfo(
                    path=full_path,
                    mode=dropbox.files.WriteMode.overwrite,
                )

                # 2. Stream remaining chunks
                while offset < file_size:
                    remaining = file_size - offset
                    chunk     = DropboxManager._read_chunk(
                        file_obj, min(CHUNK_SIZE, remaining)
                    )
                    is_last   = (offset + len(chunk)) >= file_size

                    if is_last:
                        # Final chunk — close the session
                        DropboxManager._upload_with_retry(
                            dbx.files_upload_session_finish,
                            chunk, cursor, commit,
                        )
                    else:
                        DropboxManager._upload_with_retry(
                            dbx.files_upload_session_append_v2,
                            chunk, cursor,
                        )
                        # Advance cursor AFTER a successful append
                        cursor.offset += len(chunk)

                    offset += len(chunk)

            # ── create / fetch shared link ─────────────────────────────────────
            try:
                shared = dbx.sharing_create_shared_link_with_settings(full_path)
                link   = shared.url
            except dropbox.exceptions.ApiError as api_err:
                # Link already exists — retrieve it
                try:
                    links = dbx.sharing_list_shared_links(path=full_path, direct_only=True)
                    link  = links.links[0].url if links.links else f"https://www.dropbox.com/home{full_path}"
                except Exception:
                    link = f"https://www.dropbox.com/home{full_path}"
            except Exception:
                link = f"https://www.dropbox.com/home{full_path}"

            return {
                "success":      True,
                "dropbox_path": full_path,
                "link":         link,
                "message":      "File uploaded successfully",
            }

        except exceptions.ApiError as e:
            return {
                "success":      False,
                "error":        f"Dropbox API error: {str(e)}",
                "dropbox_path": None,
                "link":         None,
            }
        except Exception as e:
            return {
                "success":      False,
                "error":        str(e),
                "dropbox_path": None,
                "link":         None,
            }

    @staticmethod
    def delete_file(dropbox_path):
        """Delete a file from Dropbox."""
        try:
            dbx = DropboxManager.get_dropbox_client()

            if not dropbox_path.startswith("/"):
                dropbox_path = "/" + dropbox_path.lstrip("/")

            dbx.files_delete_v2(dropbox_path)
            return {"success": True, "message": "File deleted successfully"}
        except Exception as e:
            return {"success": False, "error": str(e)}
