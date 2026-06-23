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


def test_router_stores_audioproc_result_without_immediate_iridium_request():
    router = Router()
    iridium_queue = Queue()
    router.register("Iridium", iridium_queue)

    message = Message(
        MessageID.ACTION_RESULT,
        {
            "action": "process",
            "result": "ok",
            "output": "data/audio_proc/audioProc_test.json",
            "data": {
                "input": "data/recordings/test.wav",
                "output": "data/audio_proc/audioProc_test.json",
            },
        },
    )

    routed = router.route("AudioProc", message)

    assert routed is False
    assert iridium_queue.empty()
    assert (
        router.latest_audio_summary["output"] == "data/audio_proc/audioProc_test.json"
    )


def test_router_does_not_route_failed_audioproc_to_iridium():
    router = Router()
    iridium_queue = Queue()
    router.register("Iridium", iridium_queue)

    message = Message(
        MessageID.ACTION_RESULT,
        {
            "action": "process",
            "result": "error",
            "error": "no_input_available",
        },
    )

    routed = router.route("AudioProc", message)

    assert routed is False
    assert iridium_queue.empty()


def test_router_does_not_route_audioproc_without_output_to_iridium():
    router = Router()
    iridium_queue = Queue()
    router.register("Iridium", iridium_queue)

    message = Message(
        MessageID.ACTION_RESULT,
        {
            "action": "process",
            "result": "ok",
            "data": {"input": "data/recordings/test.wav"},
        },
    )

    routed = router.route("AudioProc", message)

    assert routed is False
    assert iridium_queue.empty()
