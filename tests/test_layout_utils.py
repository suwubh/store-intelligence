# PROMPT: Generate unit tests for layout_builder.py utility functions focusing on resolving layout paths and listing store clip directories.
# CHANGES MADE: Mocked dependencies to prevent actual file I/O where unnecessary and covered invalid folder structures.

def test_list_store_clip_dirs(tmp_path):
    from pipeline.layout_builder import list_store_clip_dirs
    dataset_dir = tmp_path / "dataset"
    (dataset_dir / "clips" / "Store 1").mkdir(parents=True)
    (dataset_dir / "clips" / "Store 1" / "video.mp4").touch()
    
    (dataset_dir / "clips" / "Store 2").mkdir(parents=True)
    (dataset_dir / "clips" / "Store 2" / "not_video.txt").touch()
    
    dirs = list_store_clip_dirs(dataset_dir)
    assert len(dirs) == 1
    assert dirs[0].name == "Store 1"

def test_resolve_layout_path(tmp_path, monkeypatch):
    from pipeline.layout_builder import resolve_layout_path
    dataset_dir = tmp_path / "dataset"
    store_dir = dataset_dir / "clips" / "Store 1"
    store_dir.mkdir(parents=True)
    (store_dir / "video.mp4").touch()
    
    # Mock load_store_layout to prevent actual file parsing
    monkeypatch.setattr("pipeline.layout_builder.load_store_layout", lambda x: {"store_id": "ST1008"})
    
    layout_path, store_id, layout = resolve_layout_path(dataset_dir, store_folder="Store 1")
    assert store_id == "ST1008"
    assert layout_path.name == "store_layout.json"
    
    layout_path2, store_id2, layout2 = resolve_layout_path(dataset_dir, store_folder=None)
    assert store_id2 == "ST1008"
    
    explicit_json = tmp_path / "explicit.json"
    explicit_json.write_text('{"store_id": "ST9999"}')
    layout_path3, store_id3, layout3 = resolve_layout_path(dataset_dir, explicit_layout=str(explicit_json))
    assert store_id3 == "ST9999"
