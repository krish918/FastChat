"""
The gradio demo server for chatting with a single model.
"""

import argparse
from collections import defaultdict
import datetime
import json
import os
import random
import time
import uuid

import gradio as gr
import requests

from fastchat.conversation import SeparatorStyle
from fastchat.constants import (
    LOGDIR,
    WORKER_API_TIMEOUT,
    ErrorCode,
    MODERATION_MSG,
    CONVERSATION_LIMIT_MSG,
    SERVER_ERROR_MSG,
    INPUT_CHAR_LEN_LIMIT,
    CONVERSATION_LEN_LIMIT,
)
from fastchat.model.model_adapter import get_conversation_template
from fastchat.model.model_registry import model_info
from fastchat.serve.api_provider import (
    anthropic_api_stream_iter,
    bard_api_stream_iter,
    openai_api_stream_iter,
    palm_api_stream_iter,
    init_palm_chat,
)
from fastchat.serve.gradio_patch import Chatbot as grChatbot
from fastchat.serve.gradio_css import code_highlight_css
from fastchat.utils import (
    build_logger,
    violates_moderation,
    get_window_url_params_js,
)


logger = build_logger("gradio_web_server", "gradio_web_server.log")

headers = {"User-Agent": "fastchat Client"}

no_change_btn = gr.Button.update()
enable_btn = gr.Button.update(interactive=True)
disable_btn = gr.Button.update(interactive=False)

controller_url = None
enable_moderation = False

active_suggestion_category = "devops"

current_prompts = list()

suggestions = {
    "Devops": [
        """Write a Jenkins CI pipeline in groovy using shared libraries \n
compile the code in this repo https://github.com/path/to/repo\n
push the image to https://hub.docker.com/path/to/image \n
clean workspace \n
install testsuite https://github.com/test-suite/path\n
Run a smoke test using CNCF testsuite and generate a test report in .csv format\n
Run a bandit scan https://hub.docker.com/path/to/image""",
        "Write a Jenkins pipeline in groovy",
        "Print the IP address and hostname",
        "Create a workspace folder in home dir using the sudo command",
        "Provide adequate permissions to that folder",
        "Define the home dir as a variable",
        "Clone the code into the workspace https://github.com/stereolabs/zed-docker.git",
        "Print the folder path  ",
        "Docker login using token dckr_pat_BY8afnlGmK9BbqvSKMy4bHNs3S0",
        "Run a docker build in path legacy/2.8/ubuntu1804/cuda10.2/devel",
        "Push the image to https://hub.docker.com/repository/docker/pradeei/chatgpt/",
        "Clone the CNF testsuite",
        "Set the environment for the CNF testsuite",
        "Run a test using CNF testsuite and generate a nice test report in .csv & .html format",
        "Run a bandit scan https://hub.docker.com/r/path/to/image",
        "Generate a scan report in .csv format",
        "Need the pipeline to send notifications to the user post completion",
        "Cleanup the workspace",
    ],
    "Documentions": [
        """Create 300 words paragraph  
1. Ledge Park enables our developers ecosystem to quickly and easily build, test and deploy and manage solutions\n
2. distributed edge for a variety of verticals.\n
3. developer concept to implementation \n
4. various development and coding capabilities, including no-code, low-code, and significant-code\n
5. A graphical user interface with visual design tools is exposed to facilitate the no-code and low-code paradigms\n
6. built atop the Intel platform services and exposes some higher-level services of its own\n
7. Testing-as-a-Service, Build-as-a-Service, Runtime-Operational-Control-as-a-Service (ROCaaS), and Release-as-a-Service.\n
Add a catchy header and 200 words description
""",
        """
Create 300 words paragraph for Ledge Park \n
Detection -yoloX, SSD, ATSS; classification RESNET \n
1. decode, encode: H264 codec support, \n
2. preprocessing: support crop, scale, resize, \n
3. watermarks (overlay) \n
Add a catchy header and 200 words description 
""",
    ],
    "Test Cases": [
        """Given the below scenario please generate all the positive and negative test cases and test cases should be in format - Test Scenarios, Precondition, Steps and Expected Results 
Scenario: \n
Given User is logged in and navigate to AI Model page.\n 
When Creates new project with valid details. \n
Then User should be displayed with Dataset selection page and user should be able to select the dataset from available options and click Next. \n
And User should be displayed with Model Type selection page with options as ATSS, YOLOX, SSD. \n
And user click on Train button and model training should get started successfully. \n
And user should be displayed with new version for created model """,
        """Write few test cases to test Dynamic Refresh Rate Switching in windows""",
        """What is difference between Intel XE graphics and Intel Arc graphics, Provide the test cases to verify and test it""",
        """Generate all combinations of positive and negative test cases for below api and output test case should have format like Test Scenario, Preconditions, Steps and expected result \n
EndPoint: /projects \n
Method: POST \n
payload: {"name":"value", "type":"type"} \n
Valid Response Code: 200 \n
Valid Response: {"message":"Success"} \n
Invalid Response Code: 404 \n
Invalid Response: {"error": "Please provide the valid value"} \n
Invalid Response Code: 409 \n
Invalid Response: {"error": "Conflict value"} \n
Invalid Response Code: 422 \n
Invalid Response: {"error": "unprocessable entity"}""",
    ],
    "Code Review": [
        "do a code review and suggest\n improvements https://github.com/path/to/file"
    ],
    "Unit Tests": [
        "Create test 10 case names from url https://github.com/path/to/file"
    ],
}


learn_more_md = """
### License
The service is a research preview intended for non-commercial use only, subject to the model [License](https://github.com/facebookresearch/llama/blob/main/MODEL_CARD.md) of LLaMA, [Terms of Use](https://openai.com/policies/terms-of-use) of the data generated by OpenAI, and [Privacy Practices](https://chrome.google.com/webstore/detail/sharegpt-share-your-chatg/daiacboceoaocpibfodeljbdfacokfjb) of ShareGPT. Please contact us if you find any potential violation.
"""


class State:
    def __init__(self, model_name):
        self.conv = get_conversation_template(model_name)
        self.conv_id = uuid.uuid4().hex
        self.skip_next = False
        self.model_name = model_name

        if model_name == "bard":
            self.bard_session_state = {
                "conversation_id": "",
                "response_id": "",
                "choice_id": "",
                "req_id": 0,
            }
            # According to release note, "chat-bison@001" is PaLM 2 for chat.
            # https://cloud.google.com/vertex-ai/docs/release-notes#May_10_2023
            self.palm_chat = init_palm_chat("chat-bison@001")

    def to_gradio_chatbot(self):
        return self.conv.to_gradio_chatbot()

    def dict(self):
        base = self.conv.dict()
        base.update(
            {
                "conv_id": self.conv_id,
                "model_name": self.model_name,
            }
        )
        return base


def set_global_vars(controller_url_, enable_moderation_):
    global controller_url, enable_moderation
    controller_url = controller_url_
    enable_moderation = enable_moderation_


def get_conv_log_filename():
    t = datetime.datetime.now()
    name = os.path.join(LOGDIR, f"{t.year}-{t.month:02d}-{t.day:02d}-conv.json")
    return name


def get_model_list(controller_url):
    ret = requests.post(controller_url + "/refresh_all_workers")
    assert ret.status_code == 200
    ret = requests.post(controller_url + "/list_models")
    models = ret.json()["models"]
    priority = {k: f"___{i:02d}" for i, k in enumerate(model_info)}
    models.sort(key=lambda x: priority.get(x, x))
    logger.info(f"Models: {models}")
    return models


def load_demo_refresh_model_list(url_params):
    models = get_model_list(controller_url)
    selected_model = models[0] if len(models) > 0 else ""
    if "model" in url_params:
        model = url_params["model"]
        if model in models:
            selected_model = model

    dropdown_update = gr.Dropdown.update(
        choices=models, value=selected_model, visible=True
    )

    state = None
    return (
        state,
        dropdown_update,
        gr.Chatbot.update(visible=True),
        gr.Textbox.update(visible=True),
        gr.Button.update(visible=True),
        gr.Row.update(visible=True),
        gr.Accordion.update(visible=True),
    )


def load_demo_reload_model(url_params, request: gr.Request):
    logger.info(
        f"load_demo_reload_model. ip: {request.client.host}. params: {url_params}"
    )
    return load_demo_refresh_model_list(url_params)


def load_demo_single(models, url_params):
    dropdown_update = gr.Dropdown.update(visible=True)
    if "model" in url_params:
        model = url_params["model"]
        if model in models:
            dropdown_update = gr.Dropdown.update(value=model, visible=True)

    state = None
    return (
        state,
        dropdown_update,
        gr.Chatbot.update(visible=True),
        gr.Textbox.update(visible=True),
        gr.Button.update(visible=True),
        gr.Row.update(visible=True),
        gr.Accordion.update(visible=True),
    )


def load_demo(url_params, request: gr.Request):
    logger.info(f"load_demo. ip: {request.client.host}. params: {url_params}")
    return load_demo_single(models, url_params)


def vote_last_response(state, vote_type, model_selector, request: gr.Request):
    with open(get_conv_log_filename(), "a") as fout:
        data = {
            "tstamp": round(time.time(), 4),
            "type": vote_type,
            "model": model_selector,
            "state": state.dict(),
            "ip": request.client.host,
        }
        fout.write(json.dumps(data) + "\n")


def upvote_last_response(state, model_selector, request: gr.Request):
    logger.info(f"upvote. ip: {request.client.host}")
    vote_last_response(state, "upvote", model_selector, request)
    return ("",) + (disable_btn,) * 3


def downvote_last_response(state, model_selector, request: gr.Request):
    logger.info(f"downvote. ip: {request.client.host}")
    vote_last_response(state, "downvote", model_selector, request)
    return ("",) + (disable_btn,) * 3


def flag_last_response(state, model_selector, request: gr.Request):
    logger.info(f"flag. ip: {request.client.host}")
    vote_last_response(state, "flag", model_selector, request)
    return ("",) + (disable_btn,) * 3


def regenerate(state, request: gr.Request):
    logger.info(f"regenerate. ip: {request.client.host}")
    state.conv.update_last_message(None)
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 5


def clear_history(request: gr.Request):
    logger.info(f"clear_history. ip: {request.client.host}")
    state = None
    return (state, [], "") + (disable_btn,) * 5


def add_text(state, model_selector, text, request: gr.Request):
    logger.info(f"add_text. ip: {request.client.host}. len: {len(text)}")

    if state is None:
        state = State(model_selector)

    if len(text) <= 0:
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), "") + (no_change_btn,) * 5

    if enable_moderation:
        flagged = violates_moderation(text)
        if flagged:
            logger.info(f"violate moderation. ip: {request.client.host}. text: {text}")
            state.skip_next = True
            return (state, state.to_gradio_chatbot(), MODERATION_MSG) + (
                no_change_btn,
            ) * 5

    conv = state.conv
    if (len(conv.messages) - conv.offset) // 2 >= CONVERSATION_LEN_LIMIT:
        logger.info(
            f"hit conversation length limit. ip: {request.client.host}. text: {text}"
        )
        state.skip_next = True
        return (state, state.to_gradio_chatbot(), CONVERSATION_LIMIT_MSG) + (
            no_change_btn,
        ) * 5

    text = text[:INPUT_CHAR_LEN_LIMIT]  # Hard cut-off
    conv.append_message(conv.roles[0], text)
    conv.append_message(conv.roles[1], None)
    return (state, state.to_gradio_chatbot(), "") + (disable_btn,) * 5


def post_process_code(code):
    sep = "\n```"
    if sep in code:
        blocks = code.split(sep)
        if len(blocks) % 2 == 1:
            for i in range(1, len(blocks), 2):
                blocks[i] = blocks[i].replace("\\_", "_")
        code = sep.join(blocks)
    return code


def model_worker_stream_iter(
    conv,
    model_name,
    worker_addr,
    prompt,
    temperature,
    repetition_penalty,
    top_p,
    max_new_tokens,
):
    # Make requests
    gen_params = {
        "model": model_name,
        "prompt": prompt,
        "temperature": temperature,
        "repetition_penalty": repetition_penalty,
        "top_p": top_p,
        "max_new_tokens": max_new_tokens,
        "stop": conv.stop_str,
        "stop_token_ids": conv.stop_token_ids,
        "echo": False,
    }
    logger.info(f"==== request ====\n{gen_params}")

    # Stream output
    response = requests.post(
        worker_addr + "/worker_generate_stream",
        headers=headers,
        json=gen_params,
        stream=True,
        timeout=WORKER_API_TIMEOUT,
    )
    for chunk in response.iter_lines(decode_unicode=False, delimiter=b"\0"):
        if chunk:
            data = json.loads(chunk.decode())
            yield data


def http_bot(state, temperature, top_p, max_new_tokens, request: gr.Request):
    logger.info(f"http_bot. ip: {request.client.host}")
    start_tstamp = time.time()
    temperature = float(temperature)
    top_p = float(top_p)
    max_new_tokens = int(max_new_tokens)

    if state.skip_next:
        # This generate call is skipped due to invalid inputs
        state.skip_next = False
        yield (state, state.to_gradio_chatbot()) + (no_change_btn,) * 5
        return

    conv, model_name = state.conv, state.model_name
    if model_name == "gpt-3.5-turbo" or model_name == "gpt-4":
        prompt = conv.to_openai_api_messages()
        stream_iter = openai_api_stream_iter(
            model_name, prompt, temperature, top_p, max_new_tokens
        )
    elif model_name in ["claude-v1", "claude-instant-v1"]:
        prompt = conv.get_prompt()
        stream_iter = anthropic_api_stream_iter(
            model_name, prompt, temperature, top_p, max_new_tokens
        )
    elif model_name == "bard":
        # stream_iter = bard_api_stream_iter(state)
        stream_iter = palm_api_stream_iter(
            state.palm_chat, conv.messages[-2][1], temperature, top_p, max_new_tokens
        )
    else:
        # Query worker address
        ret = requests.post(
            controller_url + "/get_worker_address", json={"model": model_name}
        )
        worker_addr = ret.json()["address"]
        logger.info(f"model_name: {model_name}, worker_addr: {worker_addr}")

        # No available worker
        if worker_addr == "":
            conv.update_last_message(SERVER_ERROR_MSG)
            yield (
                state,
                state.to_gradio_chatbot(),
                disable_btn,
                disable_btn,
                disable_btn,
                enable_btn,
                enable_btn,
            )
            return

        # Construct prompt
        if "chatglm" in model_name:
            prompt = list(list(x) for x in conv.messages[conv.offset :])
        else:
            prompt = conv.get_prompt()

        # Construct repetition_penalty
        if "t5" in model_name:
            repetition_penalty = 1.2
        else:
            repetition_penalty = 1.0
        stream_iter = model_worker_stream_iter(
            conv,
            model_name,
            worker_addr,
            prompt,
            temperature,
            repetition_penalty,
            top_p,
            max_new_tokens,
        )

    conv.update_last_message("▌")
    yield (state, state.to_gradio_chatbot()) + (disable_btn,) * 5

    try:
        for data in stream_iter:
            if data["error_code"] == 0:
                output = data["text"].strip()
                if "vicuna" in model_name:
                    output = post_process_code(output)
                conv.update_last_message(output + "▌")
                yield (state, state.to_gradio_chatbot()) + (disable_btn,) * 5
            else:
                output = data["text"] + f"\n\n(error_code: {data['error_code']})"
                conv.update_last_message(output)
                yield (state, state.to_gradio_chatbot()) + (
                    disable_btn,
                    disable_btn,
                    disable_btn,
                    enable_btn,
                    enable_btn,
                )
                return
            time.sleep(0.02)
    except requests.exceptions.RequestException as e:
        conv.update_last_message(
            f"{SERVER_ERROR_MSG}\n\n"
            f"(error_code: {ErrorCode.GRADIO_REQUEST_ERROR}, {e})"
        )
        yield (state, state.to_gradio_chatbot()) + (
            disable_btn,
            disable_btn,
            disable_btn,
            enable_btn,
            enable_btn,
        )
        return
    except Exception as e:
        conv.update_last_message(
            f"{SERVER_ERROR_MSG}\n\n"
            f"(error_code: {ErrorCode.GRADIO_STREAM_UNKNOWN_ERROR}, {e})"
        )
        yield (state, state.to_gradio_chatbot()) + (
            disable_btn,
            disable_btn,
            disable_btn,
            enable_btn,
            enable_btn,
        )
        return

    # Delete "▌"
    conv.update_last_message(conv.messages[-1][-1][:-1])
    yield (state, state.to_gradio_chatbot()) + (enable_btn,) * 5

    finish_tstamp = time.time()
    logger.info(f"{output}")

    with open(get_conv_log_filename(), "a") as fout:
        data = {
            "tstamp": round(finish_tstamp, 4),
            "type": "chat",
            "model": model_name,
            "gen_params": {
                "temperature": temperature,
                "top_p": top_p,
                "max_new_tokens": max_new_tokens,
            },
            "start": round(start_tstamp, 4),
            "finish": round(finish_tstamp, 4),
            "state": state.dict(),
            "ip": request.client.host,
        }
        fout.write(json.dumps(data) + "\n")


block_css = (
    code_highlight_css
    + """
pre {
    white-space: pre-wrap;       /* Since CSS 2.1 */
    white-space: -moz-pre-wrap;  /* Mozilla, since 1999 */
    white-space: -pre-wrap;      /* Opera 4-6 */
    white-space: -o-pre-wrap;    /* Opera 7 */
    word-wrap: break-word;       /* Internet Explorer 5.5+ */
}
#notice_markdown img{
    width: 100px;
}
#notice_markdown th {
    display: none;
}
"""
)


def get_model_description_md(models):
    model_description_md = """
| | | |
| ---- | ---- | ---- |
"""
    ct = 0
    visited = set()
    for i, name in enumerate(models):
        if name in model_info:
            minfo = model_info[name]
            if minfo.simple_name in visited:
                continue
            visited.add(minfo.simple_name)
            one_model_md = f"[{minfo.simple_name}]({minfo.link}): {minfo.description}"
        else:
            visited.add(name)
            one_model_md = (
                f"[{name}](): Add the description at fastchat/model/model_registry.py"
            )

        if ct % 3 == 0:
            model_description_md += "|"
        model_description_md += f" {one_model_md} |"
        if ct % 3 == 2:
            model_description_md += "\n"
        ct += 1
    return model_description_md


def give_a_response():
    return "Hello"


def setOutputs():
    listOfSuggestions = suggestions[active_suggestion_category]
    output = []
    for suggestion in listOfSuggestions:
        output.append(gr.Button.update(value=suggestion))
    print(output)
    return output


def select_prompt_category(choice):
    global suggestions
    global current_prompts
    print("CURRENT BEFORE", current_prompts)
    if choice:
        current_prompts = suggestions.get(choice)
        print("CURRENT AFTER", current_prompts)

    return gr.Dropdown.update(
        choices=current_prompts, label=choice + " Prompts", visible=True, value=""
    )


def update_suggestions(choice):
    return choice


def execute_suggestion(index):
    print(index)
    suggestions_list = suggestions[active_suggestion_category]
    print(suggestions_list)
    return suggestions_list[index]


def build_single_model_ui(models):
    notice_markdown = """
![Intel](https://www.intel.in/content/dam/logos/intel-header-logo.svg)
# Welcome to NEX SOFTWARE Next Gen AI

"""

    state = gr.State()
    # model_description_md = get_model_description_md(models)
    gr.Markdown(notice_markdown, elem_id="notice_markdown")

    with gr.Row(elem_id="model_selector_row", visible=False) as mode_selector_row:
        model_selector = gr.Dropdown(
            choices=models,
            value=models[0] if len(models) > 0 else "",
            interactive=True,
            show_label=False,
        ).style(container=False)

    chatbot = grChatbot(
        elem_id="chatbot", label="Scroll down and start chatting", visible=False
    ).style(height=550)

    with gr.Row():
        with gr.Column(scale=10):
            suggestion_dd_input = gr.Dropdown(
                list(suggestions.keys()), label="Category"
            )
            prompt_selector = gr.Dropdown(
                list(["Option 1", "option 2", "option 3"]),
                interactive=True,
                visible=False,
            )
        with gr.Column(scale=20):
            textbox = gr.Textbox(
                show_label=False,
                placeholder="Enter text and press ENTER",
                visible=False,
            ).style(container=False)
        with gr.Column(scale=1, min_width=50):
            send_btn = gr.Button(value="Send", visible=False)

    suggestion_dd_input.change(
        fn=select_prompt_category, inputs=suggestion_dd_input, outputs=prompt_selector
    )

    prompt_selector.change(
        fn=update_suggestions, inputs=prompt_selector, outputs=textbox
    )

    with gr.Row(visible=False) as button_row:
        upvote_btn = gr.Button(value="👍  Upvote", interactive=False, visible=False)
        downvote_btn = gr.Button(value="👎  Downvote", interactive=False, visible=False)
        flag_btn = gr.Button(value="⚠️  Flag", interactive=False, visible=False)
        regenerate_btn = gr.Button(value="🔄  Regenerate", interactive=False)
        clear_btn = gr.Button(value="🗑️  Clear history", interactive=False)

    with gr.Accordion("Parameters", open=False, visible=False) as parameter_row:
        temperature = gr.Slider(
            minimum=0.0,
            maximum=1.0,
            value=0.7,
            step=0.1,
            interactive=True,
            label="Temperature",
        )
        top_p = gr.Slider(
            minimum=0.0,
            maximum=1.0,
            value=1.0,
            step=0.1,
            interactive=True,
            label="Top P",
        )
        max_output_tokens = gr.Slider(
            minimum=16,
            maximum=1024,
            value=512,
            step=64,
            interactive=True,
            label="Max output tokens",
        )

    gr.Markdown(learn_more_md)

    # Register listeners
    btn_list = [
        upvote_btn,
        downvote_btn,
        flag_btn,
        regenerate_btn,
        clear_btn,
    ]
    upvote_btn.click(
        upvote_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    downvote_btn.click(
        downvote_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    flag_btn.click(
        flag_last_response,
        [state, model_selector],
        [textbox, upvote_btn, downvote_btn, flag_btn],
    )
    regenerate_btn.click(regenerate, state, [state, chatbot, textbox] + btn_list).then(
        http_bot,
        [state, temperature, top_p, max_output_tokens],
        [state, chatbot] + btn_list,
    )
    clear_btn.click(clear_history, None, [state, chatbot, textbox] + btn_list)

    model_selector.change(clear_history, None, [state, chatbot, textbox] + btn_list)

    textbox.submit(
        add_text, [state, model_selector, textbox], [state, chatbot, textbox] + btn_list
    ).then(
        http_bot,
        [state, temperature, top_p, max_output_tokens],
        [state, chatbot] + btn_list,
    )
    send_btn.click(
        add_text, [state, model_selector, textbox], [state, chatbot, textbox] + btn_list
    ).then(
        http_bot,
        [state, temperature, top_p, max_output_tokens],
        [state, chatbot] + btn_list,
    )

    return (
        state,
        model_selector,
        chatbot,
        textbox,
        send_btn,
        button_row,
        parameter_row,
    )


def build_demo(models):
    with gr.Blocks(
        title="NEX SOFTWARE Next Gen AI",
        theme=gr.themes.Base(
            primary_hue=gr.themes.colors.neutral,
            secondary_hue=gr.themes.colors.blue,
            neutral_hue=gr.themes.colors.neutral,
        ),
        css=block_css,
    ) as demo:
        url_params = gr.JSON(visible=False)

        (
            state,
            model_selector,
            chatbot,
            textbox,
            send_btn,
            button_row,
            parameter_row,
        ) = build_single_model_ui(models)

        if args.model_list_mode == "once":
            demo.load(
                load_demo,
                [url_params],
                [
                    state,
                    model_selector,
                    chatbot,
                    textbox,
                    send_btn,
                    button_row,
                    parameter_row,
                ],
                _js=get_window_url_params_js,
            )
        elif args.model_list_mode == "reload":
            demo.load(
                load_demo_reload_model,
                [url_params],
                [
                    state,
                    model_selector,
                    chatbot,
                    textbox,
                    send_btn,
                    button_row,
                    parameter_row,
                ],
                _js=get_window_url_params_js,
            )
        else:
            raise ValueError(f"Unknown model list mode: {args.model_list_mode}")

    return demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int)
    parser.add_argument("--controller-url", type=str, default="http://localhost:21001")
    parser.add_argument("--concurrency-count", type=int, default=10)
    parser.add_argument(
        "--model-list-mode",
        type=str,
        default="once",
        choices=["once", "reload"],
        help="Whether to load the model list once or reload the model list every time.",
    )
    parser.add_argument("--share", action="store_true")
    parser.add_argument(
        "--moderate", action="store_true", help="Enable content moderation"
    )
    parser.add_argument(
        "--add-chatgpt",
        action="store_true",
        help="Add OpenAI's ChatGPT models (gpt-3.5-turbo, gpt-4)",
    )
    parser.add_argument(
        "--add-claude",
        action="store_true",
        help="Add Anthropic's Claude models (claude-v1, claude-instant-v1)",
    )
    parser.add_argument(
        "--add-bard",
        action="store_true",
        help="Add Google's Bard model (PaLM 2 for Chat: chat-bison@001)",
    )
    args = parser.parse_args()
    logger.info(f"args: {args}")

    set_global_vars(args.controller_url, args.moderate)
    models = get_model_list(args.controller_url)

    if args.add_chatgpt:
        models = ["gpt-3.5-turbo", "gpt-4"] + models
    if args.add_claude:
        models = ["claude-v1", "claude-instant-v1"] + models
    if args.add_bard:
        models = ["bard"] + models

    demo = build_demo(models)
    demo.queue(
        concurrency_count=args.concurrency_count, status_update_rate=10, api_open=False
    ).launch(
        server_name=args.host, server_port=args.port, share=args.share, max_threads=200
    )
