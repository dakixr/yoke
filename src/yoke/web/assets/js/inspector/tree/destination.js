// Selection is synchronous. Only the latest completed preview may authorize a move.
// The controller still owns HTTP request generations and server revision checks.
export class TreeDestination {
  constructor({ preview, clearPreview, navigate, changed = () => {} }) {
    this.requestPreview = preview;
    this.clearPreview = clearPreview;
    this.navigate = navigate;
    this.changed = changed;
    this.generation = 0;
    this.disposed = false;
    this.state = { targetID: null, revision: null, pending: false, preview: null, error: null, moving: false };
  }

  update(patch) {
    this.state = { ...this.state, ...patch };
    this.changed();
  }

  async select(targetID, tree) {
    if (this.disposed || this.state.moving || !tree || !targetID) return;
    const generation = ++this.generation;
    this.clearPreview();
    const current = targetID === tree.leafID;
    this.update({ targetID, revision: tree.revision, preview: null, pending: !current, error: null });
    if (current) return;
    try {
      const preview = await this.requestPreview(targetID);
      if (generation !== this.generation) return;
      if (!preview || preview.targetID !== targetID) {
        this.update({ pending: false, error: "Preview expired. Refresh it before continuing." });
        return;
      }
      this.update({ preview, pending: false });
    } catch (error) {
      if (generation === this.generation) {
        this.update({ pending: false, error: error?.message || String(error) });
      }
    }
  }

  clear() {
    if (this.disposed || this.state.moving) return;
    ++this.generation;
    this.clearPreview();
    this.update({ targetID: null, revision: null, preview: null, pending: false, error: null });
  }

  ready(tree, sharedPreview) {
    const { targetID, revision, preview, pending, moving, error } = this.state;
    return Boolean(
      !this.disposed && targetID && tree && targetID !== tree.leafID && revision === tree.revision &&
      !pending && !moving && !error && preview && !preview.current &&
      preview.targetID === targetID && sharedPreview === preview
    );
  }

  async continue(tree, sharedPreview, summary = null, { repeat = false } = {}) {
    if (repeat || !this.ready(tree, sharedPreview)) return null;
    const targetID = this.state.targetID;
    const generation = this.generation;
    this.update({ moving: true });
    try {
      const result = await this.navigate(targetID, summary || null);
      if (generation !== this.generation) return null;
      // A conflict or failed move must never leave an old preview armed.
      this.update({ preview: null, moving: false });
      if (result) this.clear();
      else this.update({ error: "Conversation changed. Refresh the preview before continuing." });
      return result;
    } catch (error) {
      if (generation !== this.generation) return null;
      this.update({ preview: null, moving: false, error: error?.message || String(error) });
      return null;
    }
  }

  dispose() {
    ++this.generation;
    this.disposed = true;
    this.changed = () => {};
  }
}
