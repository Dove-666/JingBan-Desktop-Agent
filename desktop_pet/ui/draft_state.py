"""Qt relay for one controller's local text-only draft owner."""
from collections.abc import MutableMapping
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from ..draft_owner import ArchiveDraftIdentity, LocalDraftOwner
from ..draft_store import LocalDraftStore


class DraftTextMap(MutableMapping):
    """Compatibility view; editor changes still go through one shared owner."""
    def __init__(self, owner): self.owner = owner
    def __getitem__(self, key):
        value = self.owner.snapshots().get(key)
        if value is None or not value.text: raise KeyError(key)
        return value.text
    def __iter__(self): return iter([key for key, value in self.owner.snapshots().items() if value.text])
    def __len__(self): return sum(bool(value.text) for value in self.owner.snapshots().values())
    def __setitem__(self, key, value):
        if self.owner.set_text(key, value) is None: raise ValueError("DRAFT_INVALID_ARGUMENT")
    def __delitem__(self, key):
        value = self.owner.snapshots().get(key)
        if value is None or not value.text: raise KeyError(key)
        self.owner.accepted(key, epoch=value.epoch, revision=value.revision)


def archive_identity_reader(archive):
    def read(cid):
        # Use the archive's normal idempotent initialization: on first launch
        # this background reader can beat the ordinary conversation-list job.
        # No draft schema is added to the archive. The subsequent snapshot is
        # query-only and reads no titles/content/metadata/image pixels.
        with archive._connection() as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=250")
            row = connection.execute("""SELECT
                EXISTS(SELECT 1 FROM conversations WHERE id=?),
                (SELECT id FROM messages WHERE conversation_id=?
                    ORDER BY aura_timestamp(created_at),rowid LIMIT 1),
                EXISTS(SELECT 1 FROM remote_sync_tombstones
                    WHERE entity='conversation' AND entity_id=?)""", (cid, cid, cid)).fetchone()
        return ArchiveDraftIdentity(bool(row[0]), row[1] or "", bool(row[2]))
    return read


class SharedDraftState(QObject):
    changed = Signal(str)
    loaded = Signal()

    def __init__(self, controller):
        super().__init__(controller if isinstance(controller, QObject) else None)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self.flush)
        self.changed.connect(self._edited)
        archive = getattr(controller, "store", None)
        archive_path = getattr(archive, "path", None)
        existing = getattr(controller, "_local_draft_owner", None)
        if type(existing) is LocalDraftOwner:
            self.owner = existing
            self.owner._changed, self.owner._loaded_callback = self.changed.emit, self.loaded.emit
        else:
            side = None
            reader = None
            if archive_path and str(archive_path) != ":memory:" and callable(getattr(archive, "_connection", None)):
                path = Path(archive_path)
                side = LocalDraftStore(path.with_name(path.name+".drafts.sqlite3"))
                reader = archive_identity_reader(archive)
            self.owner = LocalDraftOwner(side, identity_reader=reader,
                changed=self.changed.emit, loaded=self.loaded.emit)
            controller._local_draft_owner = self.owner
        self.texts = DraftTextMap(self.owner)
        for name in ("conversations_changed", "messages_changed"):
            signal = getattr(controller, name, None)
            if signal is not None: signal.connect(self.archive_changed)
        if isinstance(controller, QObject):
            controller.destroyed.connect(lambda: self.owner.shutdown(.1))
        app = QApplication.instance()
        if app is not None:
            # The controller owns final shutdown ordering: accepted archive
            # writes drain before owner.shutdown. Here we only queue a flush.
            app.aboutToQuit.connect(self.flush)

    @Slot(str)
    def _edited(self, _cid): self._save_timer.start()

    @Slot()
    def archive_changed(self): self.owner.archive_changed()

    @Slot()
    def flush(self):
        self._save_timer.stop()
        return self.owner.flush()


def shared_draft_state(controller):
    current = getattr(controller, "_local_draft_state", None)
    if type(current) is not SharedDraftState:
        current = SharedDraftState(controller)
        controller._local_draft_state = current
    return current
