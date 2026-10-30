import mimetypes

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.http import Http404, HttpResponse
from django.utils.deconstruct import deconstructible


@deconstructible
class DatabaseStorage(Storage):
    """Stores uploads in the StoredFile table so they survive restarts on hosts with an ephemeral disk."""

    def _model(self):
        from .models import StoredFile
        return StoredFile

    def _open(self, name, mode="rb"):
        row = self._model().objects.filter(name=name).first()
        if row is None:
            raise FileNotFoundError(name)
        return ContentFile(bytes(row.content), name=name)

    def _save(self, name, content):
        data = content.read()
        self._model().objects.create(name=name, content=data, size=len(data))
        return name

    def exists(self, name):
        return self._model().objects.filter(name=name).exists()

    def delete(self, name):
        self._model().objects.filter(name=name).delete()

    def size(self, name):
        row = self._model().objects.filter(name=name).only("size").first()
        if row is None:
            raise FileNotFoundError(name)
        return row.size

    def url(self, name):
        return f"{settings.MEDIA_URL}{name}"


def serve_stored_file(request, path):
    from .models import StoredFile
    row = StoredFile.objects.filter(name=path).first()
    if row is None:
        raise Http404
    response = HttpResponse(bytes(row.content), content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
    response["Cache-Control"] = "private, max-age=3600"
    return response
