from queue import Queue

from modules.support.base_fsm import Message, MessageID
from modules.support.router import Router


def test_router_routes_behringer_action_result_to_audioproc():
    router = Router()
    audio_queue = Queue()
    router.register("AudioProc", audio_queue)

    message = Message(
        MessageID.ACTION_RESULT,
        {
            "action": "acquire",
            "file": "/tmp/mock_recording.wav",
        },
    )

    routed = router.route("Behringer", message)

    assert routed is True

    routed_message = audio_queue.get_nowait()
    assert routed_message.id == MessageID.SIG_PROCESS
    assert routed_message.params["file"] == "/tmp/mock_recording.wav"
    assert routed_message.params["origin"] == "Behringer"


def test_router_does_not_route_when_condition_fails():
    router = Router()
    audio_queue = Queue()
    router.register("AudioProc", audio_queue)

    message = Message(
        MessageID.ACTION_RESULT,
        {
            "action": "acquire",
        },
    )

    routed = router.route("Behringer", message)

    assert routed is False
    assert audio_queue.empty()
