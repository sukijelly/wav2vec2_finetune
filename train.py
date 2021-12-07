import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional, Union
import numpy as np
import yaml
import torch
import json
from transformers import Trainer, TrainingArguments, Wav2Vec2ForCTC, Wav2Vec2Processor, Wav2Vec2CTCTokenizer, Wav2Vec2FeatureExtractor

from datasets import load_metric, load_dataset

def compute_metrics(pred, processor):
    cer_metric = load_metric("cer")
    pred_logits = pred.predictions
    pred_ids = np.argmax(pred_logits, axis=-1)
    pred.label_ids[pred.label_ids == -100] = processor.tokenizer.pad_token_id
    pred_str = processor.batch_decode(pred_ids)
    # we do not want to group tokens when computing the metrics
    label_str = processor.batch_decode(pred.label_ids, group_tokens=False)
    cer = cer_metric.compute(predictions=pred_str, references=label_str)
    return {"cer": cer}

def extract_all_chars(batch):
    all_text = " ".join(batch["transcript"])
    vocab = list(set(all_text))
    return {"vocab": [vocab], "all_text": [all_text]}

def speech_file_to_array_fn(batch):
    with open(batch["audio_path"], "rb") as audiofile:
        speech_array = np.fromfile(audiofile, dtype=np.int16).astype(np.single) / 32768
    sampling_rate = 16000
    batch["speech"] = speech_array
    batch["sampling_rate"] = sampling_rate
    batch["target_text"] = batch["transcript"]
    return batch

def make_vocab(dataset_train, dataset_test):
    vocab_train = dataset_train.map(
        extract_all_chars,
        batched=True,
        batch_size=-1,
        keep_in_memory=True,
        remove_columns=dataset_train.column_names,
    )
    vocab_test = dataset_test.map(
        extract_all_chars,
        batched=True,
        batch_size=-1,
        keep_in_memory=True,
        remove_columns=dataset_test.column_names,
    )
    vocab_list = list(set(vocab_train["vocab"][0]) | set(vocab_test["vocab"][0]))
    vocab_dict = {v: k for k, v in enumerate(vocab_list)}
    vocab_dict["|"] = vocab_dict[" "]
    del vocab_dict[" "]
    vocab_dict["[UNK]"] = len(vocab_dict)
    vocab_dict["[PAD]"] = len(vocab_dict)

    with open("vocab.json", "w") as vocab_file:
        json.dump(vocab_dict, vocab_file)

def train():
    warnings.filterwarnings("ignore", category=np.VisibleDeprecationWarning)
    warnings.filterwarnings("ignore", category=UserWarning)

    with open("config_train.yml") as f:
        args = yaml.load(f, Loader=yaml.FullLoader)

    all_dataset = load_dataset(
        "json",
        data_files={"train": args["train_data_path"], "test": args["test_data_path"]},
    )

    dataset_train = all_dataset["train"]
    dataset_test = all_dataset["test"]

    if args['make_vocab'] == True:
        make_vocab(dataset_train, dataset_test)
        print("------make_vocab_done------")

    dataset_train = dataset_train.map(
        speech_file_to_array_fn, remove_columns=dataset_train.column_names
    )
    dataset_test = dataset_test.map(
        speech_file_to_array_fn, remove_columns=dataset_test.column_names
    )

    print("------speech_file_to_array_done------")

    tokenizer = Wav2Vec2CTCTokenizer(
        args["vocab_path"],
        unk_token="[UNK]",
        pad_token="[PAD]",
        word_delimiter_token="|",
    )
    feature_extractor = Wav2Vec2FeatureExtractor(
        feature_size=1,
        sampling_rate=16000,
        padding_value=0.0,
        do_normalize=True,
        return_attention_mask=True,
    )
    processor = Wav2Vec2Processor(
        feature_extractor=feature_extractor, tokenizer=tokenizer
    )

    def prepare_dataset(batch):
        assert (
            len(set(batch["sampling_rate"])) == 1
        ), f"Make sure all inputs have the same sampling rate of {processor.feature_extractor.sampling_rate}."
        batch["input_values"] = processor(
            batch["speech"], sampling_rate=batch["sampling_rate"][0]
        ).input_values

        with processor.as_target_processor():
            batch["labels"] = processor(batch["target_text"]).input_ids
        return batch

    dataset_train = dataset_train.map(
        prepare_dataset,
        remove_columns=dataset_train.column_names,
        batch_size=4,
        num_proc=48,
        batched=True,
    )
    dataset_test = dataset_test.map(
        prepare_dataset,
        remove_columns=dataset_test.column_names,
        batch_size=4,
        num_proc=48,
        batched=True,
    )

    print("------prepare_dataset_done------")

    @dataclass
    class DataCollatorCTCWithPadding:
        processor: Wav2Vec2Processor
        padding: Union[bool, str] = True
        max_length: Optional[int] = None
        max_length_labels: Optional[int] = None
        pad_to_multiple_of: Optional[int] = None
        pad_to_multiple_of_labels: Optional[int] = None

        def __call__(
                self, features: List[Dict[str, Union[List[int], torch.Tensor]]]
        ) -> Dict[str, torch.Tensor]:
            # split inputs and labels since they have to be of different lenghts and need
            # different padding methods
            input_features = [
                {"input_values": feature["input_values"]} for feature in features
            ]
            label_features = [{"input_ids": feature["labels"]} for feature in features]
            batch = self.processor.pad(
                input_features,
                padding=self.padding,
                max_length=self.max_length,
                pad_to_multiple_of=self.pad_to_multiple_of,
                return_tensors="pt",
            )
            with self.processor.as_target_processor():
                labels_batch = self.processor.pad(
                    label_features,
                    padding=self.padding,
                    max_length=self.max_length_labels,
                    pad_to_multiple_of=self.pad_to_multiple_of_labels,
                    return_tensors="pt",
                )
            # replace padding with -100 to ignore loss correctly
            labels = labels_batch["input_ids"].masked_fill(
                labels_batch.attention_mask.ne(1), -100
            )
            batch["labels"] = labels
            return batch

    data_collator = DataCollatorCTCWithPadding(processor=processor, padding=True)

    model = Wav2Vec2ForCTC.from_pretrained(
        "facebook/wav2vec2-large-xlsr-53",
        attention_dropout=args["attention_dropout"],
        hidden_dropout=args["hidden_dropout"],
        feat_proj_dropout=args["feat_proj_dropout"],
        mask_time_prob=args["mask_time_prob"],
        layerdrop=args["layerdrop"],
        gradient_checkpointing=args["gradient_checkpointing"],
        ctc_loss_reduction=args["ctc_loss_reduction"],
        pad_token_id=processor.tokenizer.pad_token_id,
        vocab_size=len(processor.tokenizer),
    )
    model.freeze_feature_extractor()

    print("-------load_pretrained_model_done----------")

    training_args = TrainingArguments(
        output_dir=args["checkpoint_dir"],
        group_by_length=args["group_by_length"],
        per_device_train_batch_size=args["batch_size"],
        per_device_eval_batch_size=args["batch_size"],
        gradient_accumulation_steps=args["gradient_accumulation_steps"],
        evaluation_strategy=args["evaluation_strategy"],
        num_train_epochs=args["num_train_epochs"],
        fp16=args["fp16"],
        save_steps=args["save_steps"],
        eval_steps=args["eval_steps"],
        logging_steps=args["logging_steps"],
        weight_decay=args["weight_decay"],
        learning_rate=args["learning_rate"],
        warmup_steps=args["warmup_steps"],
        save_total_limit=args["save_total_limit"],
        dataloader_num_workers=args["dataloader_num_workers"],
    )

    print("-------train_ready_done---------")

    trainer = Trainer(
        model=model,
        data_collator=data_collator,
        args=training_args,
        compute_metrics=compute_metrics,
        train_dataset=dataset_train,
        eval_dataset=dataset_test,
        tokenizer=processor.feature_extractor,
    )

    print("-------training_start!---------")
    trainer.train()