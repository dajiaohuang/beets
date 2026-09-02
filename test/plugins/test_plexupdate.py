import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import pytest
import responses

from beets.exceptions import UserError
from beets.test.helper import PluginTestCase
from beetsplug._utils.requests import SingletonMeta
from beetsplug.plexupdate import PLEX_API, PlexAPI, PlexSession


class PlexUpdateTest(PluginTestCase):
    plugin = "plexupdate"

    def add_response_get_music_section(self, section_name="Music"):
        """Create response for mocking the get_music_section function."""

        responses.add(
            responses.GET,
            "http://localhost:32400/library/sections",
            status=200,
            json={
                "MediaContainer": {
                    "size": 3,
                    "Directory": [
                        {"key": "3", "type": "movie", "title": "Movies"},
                        {"key": "2", "type": "artist", "title": section_name},
                        {"key": "1", "type": "show", "title": "TV Shows"},
                    ],
                }
            },
        )

    def add_response_update_plex(self):
        """Create response for mocking the update_library request."""
        body = ""
        status = 200
        content_type = "text/html"

        responses.add(
            responses.GET,
            "http://localhost:32400/library/sections/2/refresh",
            body=body,
            status=status,
            content_type=content_type,
        )

    def setUp(self):
        super().setUp()

        self.config["plex"] = {"host": "localhost", "port": 32400}
        self.tempdir = tempfile.mkdtemp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tempdir)
        # PlexSession is a singleton; drop it so other tests get a
        # session bound to their own token path.
        SingletonMeta._instances.pop(PlexSession, None)

    def api(self, **kwargs) -> PlexAPI:
        """PlexAPI bound to the test configuration."""
        options = {
            "token_override": "",
            "host": "localhost",
            "port": 32400,
            "library_name": "Music",
            "secure": False,
            "verify": True,
            "token_path": os.path.join(self.tempdir, "plex_token.json"),
        }
        options.update(kwargs)
        return PlexAPI(**options)

    @responses.activate
    def test_get_music_section(self):
        # Adding response.
        self.add_response_get_music_section()

        # Test if section key is "2" out of the mocking data.
        assert self.api().get_music_section() == "2"

    @responses.activate
    def test_get_named_music_section(self):
        # Adding response.
        self.add_response_get_music_section("My Music Library")

        assert (
            self.api(library_name="My Music Library").get_music_section() == "2"
        )

    @responses.activate
    def test_update_library(self):
        # Adding responses.
        self.add_response_get_music_section()
        self.add_response_update_plex()

        # Testing status code of the mocking request.
        assert self.api().update_library().status_code == 200

    @responses.activate
    def test_update_library_missing_section(self):
        # The Plex server has no section with the configured name.
        self.add_response_get_music_section("Other Library")

        with pytest.raises(UserError, match="No music library named"):
            self.api().update_library()


class PlexAuthTestBase(unittest.TestCase):
    """Shared setup and mocked plex.tv responses for the auth tests."""

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.token_path = os.path.join(self.tempdir, "plex_token.json")

    def tearDown(self):
        shutil.rmtree(self.tempdir)
        # PlexSession is a singleton; drop it so each test gets a session
        # bound to its own token path.
        SingletonMeta._instances.pop(PlexSession, None)

    def add_response_create_pin(self):
        responses.add(
            responses.POST,
            f"{PLEX_API}/pins",
            status=200,
            json={"id": 1, "code": "ABCD", "expiresIn": 1800},
        )

    def add_response_poll_pin(self, auth_token=None):
        data = {"id": 1, "code": "ABCD"}
        if auth_token:
            data["authToken"] = auth_token
        responses.add(
            responses.GET, f"{PLEX_API}/pins/1", status=200, json=data
        )


class PlexSessionTest(PlexAuthTestBase):
    def setUp(self):
        super().setUp()
        self.session = PlexSession(token_path=self.token_path)

    def test_session_headers(self):
        """The session sends the identifying Plex headers."""
        assert self.session.headers["X-Plex-Product"] == "beets"
        assert (
            self.session.headers["X-Plex-Client-Identifier"]
            == self.session.client_id
        )

    @responses.activate
    def test_wait_for_login_timeout(self):
        # The pin never gets an authToken.
        self.add_response_poll_pin()

        with mock.patch("beetsplug.plexupdate.time.sleep"):
            assert self.session.wait_for_login(1, timeout=1) is None

    def test_loads_stored_token(self):
        """The token is read from the token file."""
        with open(self.token_path, "w") as f:
            json.dump(
                {"X-Plex-Token": "TOKEN", "client_identifier": "prev-id"}, f
            )

        assert self.session.token == "TOKEN"

    def test_session_saves_token(self):
        """Token persistence is handled by the session."""
        self.session.save_token({"X-Plex-Token": "TOKEN123"})

        assert self.session.token == "TOKEN123"
        assert self.session.load_token() == {
            "X-Plex-Token": "TOKEN123",
            "client_identifier": self.session.client_id,
        }

    @responses.activate
    def test_request_attaches_token_to_local_server(self):
        """The stored token is sent to the local server."""
        responses.add(
            responses.GET,
            "http://localhost:32400/library/sections",
            status=200,
            json={},
        )
        self.session.save_token({"X-Plex-Token": "TOKEN123"})

        self.session.get("http://localhost:32400/library/sections")

        assert responses.calls[0].request.headers["X-Plex-Token"] == "TOKEN123"

    @responses.activate
    def test_request_prefers_configured_token(self):
        """The configured token wins over the stored plex.tv token."""
        SingletonMeta._instances.pop(PlexSession, None)
        session = PlexSession(
            token_path=self.token_path, token_override="CONFIG_TOKEN"
        )
        session.save_token({"X-Plex-Token": "TOKEN123"})
        responses.add(
            responses.GET,
            "http://localhost:32400/library/sections",
            status=200,
            json={},
        )

        session.get("http://localhost:32400/library/sections")

        assert (
            responses.calls[0].request.headers["X-Plex-Token"] == "CONFIG_TOKEN"
        )


class PlexAPITest(PlexAuthTestBase):
    def setUp(self):
        super().setUp()
        self.api = PlexAPI(
            token_path=self.token_path,
            token_override="",
            host="localhost",
            port=32400,
            library_name="Music",
            secure=False,
            verify=True,
        )

    @responses.activate
    def test_ui_authenticate_flow(self):
        self.add_response_create_pin()
        # First poll is still pending, second grants access.
        self.add_response_poll_pin()
        self.add_response_poll_pin(auth_token="TOKEN123")

        with (
            mock.patch("beetsplug.plexupdate.time.sleep"),
            mock.patch("beetsplug.plexupdate.webbrowser.open") as open_mock,
        ):
            self.api.ui_authenticate_flow()

        open_mock.assert_called_once()

        # The pin request carries the identifying headers.
        request = responses.calls[0].request
        assert (
            request.headers["X-Plex-Client-Identifier"]
            == self.api.session.client_id
        )

        # The access token is persisted for later runs.
        assert self.api.session.token == "TOKEN123"
        with open(self.token_path) as f:
            data = json.load(f)
        assert data["X-Plex-Token"] == "TOKEN123"
        assert data["client_identifier"] == self.api.session.client_id

    @responses.activate
    def test_ui_authenticate_flow_timeout(self):
        self.add_response_create_pin()

        with (
            mock.patch("beetsplug.plexupdate.time.sleep"),
            mock.patch("beetsplug.plexupdate.webbrowser.open"),
            mock.patch.object(
                self.api.session, "wait_for_login", return_value=None
            ),
        ):
            with pytest.raises(UserError):
                self.api.ui_authenticate_flow()

        # No token is stored when the login is not completed.
        assert self.api.session.token is None

    @responses.activate
    def test_ui_authenticate_flow_network_error(self):
        # A failing plex.tv request surfaces as a UserError instead of a
        # raw requests exception.
        responses.add(responses.POST, f"{PLEX_API}/pins", status=500)

        with pytest.raises(UserError, match="Plex login flow failed"):
            self.api.ui_authenticate_flow()
