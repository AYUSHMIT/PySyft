# relative
from ...serde.serializable import serializable
from ...store.document_store import BaseUIDStoreStash
from ...store.document_store import DocumentStore
from ...store.document_store import PartitionSettings
from ...types.blob_storage import FileObject


@serializable()
class BlobStorageStash(BaseUIDStoreStash):
    object_type = FileObject
    settings: PartitionSettings = PartitionSettings(
        name=FileObject.__canonical_name__, object_type=FileObject
    )

    def __init__(self, store: DocumentStore) -> None:
        super().__init__(store=store)
