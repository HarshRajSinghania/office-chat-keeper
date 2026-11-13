# NOTA (office-chat-keeper): cópia do arquivo original com uma única alteração,
# descrita em _vendor/README.md: a classe deixou de herdar de ArtifactLocationProtocol (módulo não embutido).
import typing



class ArtifactLocation:
    def __init__(self, source_file: str, offset: typing.Optional[int], friendly_string: str):
        self._source_file = source_file
        self._offset = offset
        self._friendly_string = friendly_string

    @property
    def source_file(self) -> str:
        return self._source_file

    @property
    def offset(self) -> typing.Optional[int]:
        return self._offset

    @property
    def friendly_string(self) -> str:
        return self._friendly_string

    def __str__(self):
        return self._friendly_string
