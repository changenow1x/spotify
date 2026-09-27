

class TestEnvironmentOverrides:
    """Three env vars exist for running the app unattended.

    They are the only supported way to redirect the install away from the
    default location, so they are pinned here rather than left to chance.
    """

    def _write(self, tmp_path, name="Antike Spiritualitaet"):
        path = tmp_path / "config" / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'root: ""\nhash_mode: quick\n'
            f'shows:\n  - name: "{name}"\n    id: "show-1"\n',
            encoding="utf-8")
        return path

    def test_podcast_root_redirects_the_whole_tree(self, tmp_path, monkeypatch):
        from app.core.config import load_config

        config_file = self._write(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        monkeypatch.setenv("PODCAST_ROOT", str(elsewhere))
        cfg = load_config(config_file)
        assert cfg.root_path() == elsewhere
        assert cfg.shows_dir() == elsewhere / "shows"

    def test_podcast_port_reconfigures_the_dashboard(self, tmp_path, monkeypatch):
        from app.core.config import load_config

        config_file = self._write(tmp_path)
        monkeypatch.setenv("PODCAST_PORT", "9999")
        assert load_config(config_file).api.port == 9999

    def test_podcast_headless_switches_the_browser(self, tmp_path, monkeypatch):
        from app.core.config import load_config

        config_file = self._write(tmp_path)
        monkeypatch.setenv("PODCAST_HEADLESS", "true")
        assert load_config(config_file).spotify.headless is True
        monkeypatch.setenv("PODCAST_HEADLESS", "no")
        assert load_config(config_file).spotify.headless is False
