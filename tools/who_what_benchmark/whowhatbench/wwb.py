import argparse
import difflib
import numpy as np
import logging
import os

from transformers import AutoTokenizer, AutoProcessor, AutoConfig
import openvino as ov

import pandas as pd
from datasets import load_dataset
from PIL import Image

from whowhatbench.model_loaders import load_model
from whowhatbench import EVALUATOR_REGISTRY

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="WWB CLI",
        description="This script generates answers for questions from csv file",
    )

    parser.add_argument(
        "--base-model",
        default=None,
        help="Model for ground truth generation.",
    )
    parser.add_argument(
        "--target-model",
        default=None,
        help="Model to compare against the base_model. Usually it is compressed, quantized version of base_model.",
    )
    parser.add_argument(
        "--tokenizer",
        default=None,
        help="Tokenizer for divergency metric. If not provided, it will be load from base_model or target_model.",
    )
    parser.add_argument(
        "--chat-template",
        action="store_true",
        help="Whether apply the default chat template.",
    )
    parser.add_argument(
        "--gt-data",
        default=None,
        help="CSV file containing GT outputs from --base-model. If defined and exists then --base-model will not used."
        " If the files does not exist, it will be generated by --base-model evaluation.",
    )
    parser.add_argument(
        "--target-data",
        default=None,
        help="CSV file containing outputs from target model. If defined and exists then --target-model will not used."
        " If the files does not exist, it will be generated by --target-model evaluation.",
    )
    parser.add_argument(
        "--model-type",
        type=str,
        choices=["text", "text-to-image", "visual-text", "image-to-image", "image-inpainting"],
        default="text",
        help="Indicated the model type: 'text' - for causal text generation, 'text-to-image' - for image generation, "
        "visual-text - for Visual Language Models, image-to-image - for image generation based on image and prompt",
    )
    parser.add_argument(
        "--data-encoder",
        type=str,
        default="sentence-transformers/all-mpnet-base-v2",
        help="Model for measurement of similarity between base_model and target_model."
        " By default it is sentence-transformers/all-mpnet-base-v2,"
        " but for Chinese LLMs, better to use sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2.",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=None,
        help="Name of the dataset with prompts. The interface for dataset is load_dataset from datasets library."
        " Please provide this argument in format path,name (for example wikitext,wikitext-2-v1)."
        " If None then internal list of prompts will be used.",
    )
    parser.add_argument(
        "--dataset-field",
        type=str,
        default="text",
        help="The name of field in dataset for prompts. For example question or context in squad."
        " Will be used only if dataset is defined.",
    )
    parser.add_argument(
        "--split",
        type=str,
        default=None,
        help="Split of prompts from dataset (for example train, validation, train[:32])."
        " Will be used only if dataset is defined.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Directory name for saving the per sample comparison and metrics in CSV files.",
    )
    parser.add_argument(
        "--num-samples",
        type=int,
        default=None,
        help="Maximum number of prompts to use from dataset",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print results and their difference",
    )
    parser.add_argument(
        "--device",
        type=str,
        default="CPU",
        help="Device to run the model, e.g. 'CPU', 'GPU'.",
    )
    parser.add_argument(
        "--ov-config",
        type=str,
        default=None,
        help="Path to the JSON file that contains OpenVINO Runtime configuration.",
    )
    parser.add_argument(
        "--language",
        type=str,
        choices=["en", "cn"],
        default="en",
        help="Used to select default prompts based on the primary model language, e.g. 'en', 'cn'.",
    )
    parser.add_argument(
        "--hf",
        action="store_true",
        help="Use AutoModelForCausalLM from transformers library to instantiate the model.",
    )
    parser.add_argument(
        "--genai",
        action="store_true",
        help="Use LLMPipeline from transformers library to instantiate the model.",
    )
    parser.add_argument(
        "--cb-config",
        type=str,
        default=None,
        help="Path to the JSON file that contains SchedulerConfig for Continuous Batching Pipeline"
        "of OpenVINO GenAI API.",
    )
    parser.add_argument(
        "--llamacpp",
        action="store_true",
        help="Use llama-cpp-python to instantiate the model.",
    )
    parser.add_argument(
        "--image-size",
        type=int,
        default=None,
        help="Text-to-image specific parameter that defines the image resolution.",
    )
    parser.add_argument(
        "--num-inference-steps",
        type=int,
        default=4,
        help="Text-to-image specific parameter that defines the number of denoising steps.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Text-to-image specific parameter that defines the seed value.",
    )

    return parser.parse_args()


def check_args(args):
    if args.base_model is None and args.gt_data is None:
        raise ValueError("Wether --base-model or --gt-data should be provided")
    if args.target_model is None and args.gt_data is None and args.target_data:
        raise ValueError(
            "Wether --target-model, --target-data or --gt-data should be provided")


def load_prompts(args):
    if args.dataset is None:
        return None
    split = "validation"
    if args.split is not None:
        split = args.split
    if "," in args.dataset:
        path_name = args.dataset.split(",")
        path = path_name[0]
        name = path_name[1]
    else:
        path = args.dataset
        name = None
    data = load_dataset(path=path, name=name, split=split)

    res = data[args.dataset_field]
    res = {"prompts": list(res)}
    return res


def load_tokenizer(args):
    tokenizer = None
    if args.tokenizer is not None:
        if args.llamacpp:
            from llama_cpp.llama_tokenizer import LlamaHFTokenizer
            tokenizer = LlamaHFTokenizer.from_pretrained(args.tokenizer)
        else:
            tokenizer = AutoTokenizer.from_pretrained(
                args.tokenizer, trust_remote_code=True
            )
    elif args.base_model is not None:
        tokenizer = AutoTokenizer.from_pretrained(
            args.base_model, trust_remote_code=True
        )
    elif args.target_model is not None:
        tokenizer = AutoTokenizer.from_pretrained(
            args.target_model, trust_remote_code=True
        )

    return tokenizer


def load_processor(args):
    model_id = args.base_model if args.base_model is not None else args.target_model
    if model_id is None:
        return None

    config = AutoConfig.from_pretrained(model_id, trust_remote_code=True)
    if "llava-qwen" in config.model_type:
        preprocessor_id = config.mm_vision_tower
    else:
        preprocessor_id = model_id

    return AutoProcessor.from_pretrained(
        preprocessor_id, trust_remote_code=True
    )


def diff_strings(a: str, b: str, *, use_loguru_colors: bool = False) -> str:
    output = []
    matcher = difflib.SequenceMatcher(None, a, b)
    if use_loguru_colors:
        green = "<GREEN><black>"
        red = "<RED><black>"
        endgreen = "</black></GREEN>"
        endred = "</black></RED>"
    else:
        green = "\x1b[38;5;16;48;5;2m"
        red = "\x1b[38;5;16;48;5;1m"
        endgreen = "\x1b[0m"
        endred = "\x1b[0m"

    for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
        if opcode == "equal":
            output.append(a[a0:a1])
        elif opcode == "insert":
            output.append(f"{green}{b[b0:b1]}{endgreen}")
        elif opcode == "delete":
            output.append(f"{red}{a[a0:a1]}{endred}")
        elif opcode == "replace":
            output.append(f"{green}{b[b0:b1]}{endgreen}")
            output.append(f"{red}{a[a0:a1]}{endred}")
    return "".join(output)


def genai_gen_text(model, tokenizer, question, max_new_tokens, skip_question, use_chat_template=False):
    return model.generate(question, do_sample=False, max_new_tokens=max_new_tokens, apply_chat_template=use_chat_template)


def llamacpp_gen_text(model, tokenizer, question, max_new_tokens, skip_question, use_chat_template=False):
    if use_chat_template:
        output = model.create_chat_completion(messages=[{"role": "user", "content": question}], max_tokens=max_new_tokens, temperature=0.0)
        text = output["choices"][0]["message"]["content"]
        if skip_question:
            text = text[len(question):]
        return text
    else:
        output = model(question, max_tokens=max_new_tokens, echo=True, temperature=0.0)
        text = output["choices"][0]["text"]
        if skip_question:
            text = text[len(question):]
        return text


def genai_gen_image(model, prompt, num_inference_steps, generator=None):
    if model.resolution is not None and model.resolution[0] is not None:
        image_tensor = model.generate(
            prompt,
            width=model.resolution[0],
            height=model.resolution[1],
            num_inference_steps=num_inference_steps,
            generator=generator,
        )
    else:
        image_tensor = model.generate(
            prompt,
            num_inference_steps=num_inference_steps,
            generator=generator,
        )
    image = Image.fromarray(image_tensor.data[0])
    return image


def genai_gen_image2image(model, prompt, image, num_inference_steps, generator=None):
    image_data = ov.Tensor(np.array(image)[None])
    image_tensor = model.generate(
        prompt,
        image=image_data,
        num_inference_steps=num_inference_steps,
        strength=0.8,
        generator=generator,
    )
    image = Image.fromarray(image_tensor.data[0])
    return image


def genai_gen_inpainting(model, prompt, image, mask, num_inference_steps, generator=None):
    image_data = ov.Tensor(np.array(image)[None])
    mask_data = ov.Tensor(np.array(mask)[None])
    image_tensor = model.generate(
        prompt,
        image=image_data,
        mask_image=mask_data,
        num_inference_steps=num_inference_steps,
        generator=generator,
    )
    image = Image.fromarray(image_tensor.data[0])
    return image


def genai_gen_visual_text(model, prompt, image, processor, tokenizer, max_new_tokens, crop_question):
    image_data = ov.Tensor(np.array(image)[None])
    out = model.generate(prompt, image=image_data, do_sample=False, max_new_tokens=max_new_tokens)
    return out.texts[0]


def create_evaluator(base_model, args):
    # config = AutoConfig.from_pretrained(model_id, trust_remote_code=True)
    # task = TasksManager.infer_task_from_model(config._name_or_path)
    # TODO: Add logic to auto detect task based on model_id (TaskManager does not work for locally saved models)
    task = args.model_type

    try:
        EvaluatorCLS = EVALUATOR_REGISTRY[task]
        prompts = load_prompts(args)

        if task == "text":
            tokenizer = load_tokenizer(args) if not args.llamacpp else None

            if args.genai:
                gen_answer_fn = genai_gen_text
            elif args.llamacpp:
                gen_answer_fn = llamacpp_gen_text
            else:
                gen_answer_fn = None

            return EvaluatorCLS(
                base_model=base_model,
                gt_data=args.gt_data,
                test_data=prompts,
                tokenizer=tokenizer,
                similarity_model_id=args.data_encoder,
                num_samples=args.num_samples,
                language=args.language,
                gen_answer_fn=gen_answer_fn,
                use_chat_template=args.chat_template,
            )
        elif task == "text-to-image":
            return EvaluatorCLS(
                base_model=base_model,
                gt_data=args.gt_data,
                test_data=prompts,
                num_samples=args.num_samples,
                resolution=(args.image_size, args.image_size),
                num_inference_steps=args.num_inference_steps,
                gen_image_fn=genai_gen_image if args.genai else None,
                is_genai=args.genai,
                seed=args.seed,
            )
        elif task == "visual-text":
            tokenizer = load_tokenizer(args)
            processor = load_processor(args)
            return EvaluatorCLS(
                base_model=base_model,
                gt_data=args.gt_data,
                test_data=prompts,
                tokenizer=tokenizer,
                num_samples=args.num_samples,
                similarity_model_id=args.data_encoder,
                gen_answer_fn=genai_gen_visual_text if args.genai else None,
                processor=processor,
            )
        elif task == "image-to-image":
            return EvaluatorCLS(
                base_model=base_model,
                gt_data=args.gt_data,
                test_data=prompts,
                num_samples=args.num_samples,
                num_inference_steps=args.num_inference_steps,
                gen_image_fn=genai_gen_image2image if args.genai else None,
                is_genai=args.genai,
                seed=args.seed,
            )
        elif task == "image-inpainting":
            return EvaluatorCLS(
                base_model=base_model,
                gt_data=args.gt_data,
                test_data=prompts,
                num_samples=args.num_samples,
                num_inference_steps=args.num_inference_steps,
                gen_image_fn=genai_gen_inpainting if args.genai else None,
                is_genai=args.genai,
                seed=args.seed,
            )
        else:
            raise ValueError(f"Unsupported task: {task}")

    except KeyError as e:
        raise ValueError(
            f"Attempted to load evaluator for '{task}', but no evaluator for this model type found!"
            "Supported model types: {', '.join(EVALUATOR_REGISTRY.keys())}. Details:\n",
            e
        )


def print_text_results(evaluator):
    metric_of_interest = "similarity"
    worst_examples = evaluator.worst_examples(
        top_k=5, metric=metric_of_interest)
    for i, e in enumerate(worst_examples):
        ref_text = ""
        actual_text = ""
        diff = ""
        for l1, l2 in zip(
            e["source_model"].splitlines(), e["optimized_model"].splitlines()
        ):
            if l1 == "" and l2 == "":
                continue
            ref_text += l1 + "\n"
            actual_text += l2 + "\n"
            diff += diff_strings(l1, l2) + "\n"

        logger.info(
            "======================================================================================================="
        )
        logger.info("## Prompt %d:\n%s\n", i + 1, e["prompt"])
        logger.info("## Metric value:%.4f\n", e[metric_of_interest])
        logger.info("## Reference text:\n%s\n", ref_text)
        logger.info("## Actual text:\n%s\n", actual_text)
        logger.info("## Diff:\n%s\n", diff)


def print_image_results(evaluator):
    metric_of_interest = "similarity"
    pd.set_option('display.max_colwidth', None)
    worst_examples = evaluator.worst_examples(
        top_k=5, metric=metric_of_interest)
    for i, e in enumerate(worst_examples):
        logger.info(
            "======================================================================================================="
        )
        logger.info(f"Top-{i+1} example:")
        logger.info(e)


def read_cb_config(path):
    import json

    try:
        with open(path, 'r') as f:
            config = json.load(f)
        return config
    except FileNotFoundError:
        logger.error(f"Configuration file not found at: {path}")
        return {}
    except json.JSONDecodeError:
        logger.error(f"Invalid JSON format in configuration file: {path}")
        return {}


def main():
    args = parse_args()
    check_args(args)

    kwargs = {}
    if args.cb_config:
        kwargs["cb_config"] = read_cb_config(args.cb_config)

    if args.gt_data and os.path.exists(args.gt_data):
        evaluator = create_evaluator(None, args)
    else:
        base_model = load_model(
            args.model_type,
            args.base_model,
            args.device,
            args.ov_config,
            args.hf,
            args.genai,
            **kwargs,
        )
        evaluator = create_evaluator(base_model, args)

        if args.gt_data:
            evaluator.dump_gt(args.gt_data)
        del base_model

    if args.target_data or args.target_model:
        if args.target_data and os.path.exists(args.target_data):
            all_metrics_per_question, all_metrics = evaluator.score(
                args.target_data,
                None,
                output_dir=args.output
            )
        else:
            target_model = load_model(
                args.model_type,
                args.target_model,
                args.device,
                args.ov_config,
                args.hf,
                args.genai,
                args.llamacpp,
                **kwargs
            )
            all_metrics_per_question, all_metrics = evaluator.score(
                target_model,
                evaluator.get_generation_fn() if args.genai or args.llamacpp else None,
                output_dir=args.output
            )
        logger.info("Metrics for model: %s", args.target_model)
        logger.info(all_metrics)

        if args.output:
            if not os.path.exists(args.output):
                os.mkdir(args.output)
            df = pd.DataFrame(all_metrics_per_question)
            df.to_csv(os.path.join(args.output, "metrics_per_qustion.csv"))
            df = pd.DataFrame(all_metrics)
            df.to_csv(os.path.join(args.output, "metrics.csv"))
            evaluator.dump_predictions(os.path.join(args.output, "target.csv"))

    if args.verbose and (args.target_model or args.target_data):
        if args.model_type == "text" or args.model_type == "visual-text":
            print_text_results(evaluator)
        elif "text-to-image" in args.model_type or "image-to-image" in args.model_type:
            print_image_results(evaluator)


if __name__ == "__main__":
    main()
