import ast
import functools
import json
import logging
import math
import os
import random
import re
import sys
from dataclasses import dataclass
from multiprocessing import Value


import braceexpand
import torch
import torchvision
import webdataset as wds
from nltk import sent_tokenize
from PIL import Image
from torch.utils.data import DataLoader, IterableDataset, get_worker_info
from torch.utils.data.distributed import DistributedSampler
import base64
from webdataset.filters import _shuffle
from webdataset.tariterators import (
    base_plus_ext,
    tar_file_expander,
    url_opener,
    valid_sample,
)

from PIL import Image
import io


Image.MAX_IMAGE_PIXELS = 1000000000
MAX_NUM_TOKENS = 256
MAX_NUM_IMAGES = 5
TINY_IMAGE_SIZE_THRESHOLD = 1
N_CHANNELS = 3
INTERLEAVED_IMAGE_SIZE = 224


try:
    import horovod.torch as hvd
except ImportError:
    hvd = None


class SharedEpoch:
    def __init__(self, epoch: int = 0):
        self.shared_epoch = Value("i", epoch)

    def set_value(self, epoch):
        self.shared_epoch.value = epoch

    def get_value(self):
        return self.shared_epoch.value


@dataclass
class DataInfo:
    dataloader: DataLoader
    sampler: DistributedSampler = None
    shared_epoch: SharedEpoch = None

    def set_epoch(self, epoch):
        if self.shared_epoch is not None:
            self.shared_epoch.set_value(epoch)
        if self.sampler is not None and isinstance(self.sampler, DistributedSampler):
            self.sampler.set_epoch(epoch)


def get_dataset_size(shards):
    shards_list = list(braceexpand.braceexpand(shards))
    dir_path = os.path.dirname(shards)
    sizes_filename = os.path.join(dir_path, "sizes.json")
    len_filename = os.path.join(dir_path, "__len__")
    if os.path.exists(sizes_filename):
        sizes = json.load(open(sizes_filename, "r"))
        total_size = sum(
            [
                int(sizes[os.path.basename(shard)])
                if os.path.basename(shard) in sizes
                else 0
                for shard in shards_list
            ]
        )
    elif os.path.exists(len_filename):
        # FIXME this used to be eval(open(...)) but that seemed rather unsafe
        total_size = ast.literal_eval(open(len_filename, "r").read())
    else:
        total_size = None  # num samples undefined
        # some common dataset sizes (at time of authors last download)
        # CC3M (train): 2905954
        # CC12M: 10968539
        # LAION-400M: 407332084
        # LAION-2B (english): 2170337258
    num_shards = len(shards_list)
    return total_size, num_shards


def count_samples(dataloader):
    os.environ["WDS_EPOCH"] = "0"
    n_elements, n_batches = 0, 0
    for images, texts in dataloader:
        n_batches += 1
        n_elements += len(images)
        assert len(images) == len(texts)
    return n_elements, n_batches


def filter_no_caption_or_no_image(sample):
    return ("txt" in sample) and (
        "png" in sample or "jpg" in sample or "jpeg" in sample
    )


def log_and_continue(exn):
    """Call in an exception handler to ignore any exception, issue a warning, and continue."""
    logging.warning(f"Handling webdataset error ({repr(exn)}). Ignoring.")
    return True


def group_by_keys_nothrow(
    data, keys=base_plus_ext, lcase=True, suffixes=None, handler=None
):
    """Return function over iterator that groups key, value pairs into samples.

    :param keys: function that splits the key into key and extension (base_plus_ext)
    :param lcase: convert suffixes to lower case (Default value = True)
    """
    current_sample = None
    for filesample in data:
        assert isinstance(filesample, dict)
        fname, value = filesample["fname"], filesample["data"]
        prefix, suffix = keys(fname)
        if prefix is None:
            continue
        if lcase:
            suffix = suffix.lower()
        # FIXME webdataset version throws if suffix in current_sample, but we have a potential for
        #  this happening in the current LAION400m dataset if a tar ends with same prefix as the next
        #  begins, rare, but can happen since prefix aren't unique across tar files in that dataset
        if (
            current_sample is None
            or prefix != current_sample["__key__"]
            or suffix in current_sample
        ):
            if valid_sample(current_sample):
                yield current_sample
            current_sample = dict(__key__=prefix, __url__=filesample["__url__"])
        if suffixes is None or suffix in suffixes:
            current_sample[suffix] = value
    if valid_sample(current_sample):
        yield current_sample


def tarfile_to_samples_nothrow(src, handler=log_and_continue):
    # NOTE this is a re-impl of the webdataset impl with group_by_keys that doesn't throw
    streams = url_opener(src, handler=handler)
    files = tar_file_expander(streams, handler=handler)
    samples = group_by_keys_nothrow(files, handler=handler)
    return samples


def pytorch_worker_seed():
    """get dataloader worker seed from pytorch"""
    worker_info = get_worker_info()
    if worker_info is not None:
        # favour the seed already created for pytorch dataloader workers if it exists
        return worker_info.seed
    # fallback to wds rank based seed
    return wds.utils.pytorch_worker_seed()


_SHARD_SHUFFLE_SIZE = 2000
_SHARD_SHUFFLE_INITIAL = 500
_SAMPLE_SHUFFLE_SIZE = 5000
_SAMPLE_SHUFFLE_INITIAL = 1000


class detshuffle2(wds.PipelineStage):
    def __init__(
        self,
        bufsize=1000,
        initial=100,
        seed=0,
        epoch=-1,
    ):
        self.bufsize = bufsize
        self.initial = initial
        self.seed = seed
        self.epoch = epoch

    def run(self, src):
        if isinstance(self.epoch, SharedEpoch):
            epoch = self.epoch.get_value()
        else:
            # NOTE: this is epoch tracking is problematic in a multiprocess (dataloader workers or train)
            # situation as different workers may wrap at different times (or not at all).
            self.epoch += 1
            epoch = self.epoch
        rng = random.Random()
        if self.seed < 0:
            seed = pytorch_worker_seed() + epoch
        else:
            seed = self.seed + epoch
        rng.seed(seed)
        return _shuffle(src, self.bufsize, self.initial, rng)


class ResampledShards2(IterableDataset):
    """An iterable dataset yielding a list of urls."""

    def __init__(
        self,
        urls,
        nshards=sys.maxsize,
        worker_seed=None,
        deterministic=False,
        epoch=-1,
    ):
        """Sample shards from the shard list with replacement.

        :param urls: a list of URLs as a Python list or brace notation string
        """
        super().__init__()
        urls = wds.shardlists.expand_urls(urls)
        self.urls = urls
        assert isinstance(self.urls[0], str)
        self.nshards = nshards
        self.rng = random.Random()
        self.worker_seed = pytorch_worker_seed if worker_seed is None else worker_seed
        self.deterministic = deterministic
        self.epoch = epoch

    def __iter__(self):
        """Return an iterator over the shards."""
        if isinstance(self.epoch, SharedEpoch):
            epoch = self.epoch.get_value()
        else:
            # NOTE: this is epoch tracking is problematic in a multiprocess (dataloader workers or train)
            # situation as different workers may wrap at different times (or not at all).
            self.epoch += 1
            epoch = self.epoch

        if self.deterministic:
            # reset seed w/ epoch if deterministic, worker seed should be deterministic due to arg.seed
            self.rng.seed(self.worker_seed() + epoch)
        for _ in range(self.nshards):
            yield dict(url=self.rng.choice(self.urls))


def preprocess_image(sample, image_processor):
    image = image_processor(images=sample, return_tensors="pt")["pixel_values"]
    # apply random horizontal flip and color jitter
    image = torchvision.transforms.RandomHorizontalFlip(p=0.5)(image)
    image = torchvision.transforms.ColorJitter(brightness=0.5, hue=0.3)(image)
    return image


def preprocess_text(sample, tokenizer):
    tokenizer.padding_side = "right"
    sample = [
        (f"<image>{s.strip()}<|endofchunk|>{tokenizer.eos_token}") for s in sample
    ]
    text = tokenizer(
        sample,
        max_length=32,
        padding="longest",
        truncation="only_first",
        return_tensors="pt",
    )
    return text["input_ids"], text["attention_mask"]


def preprocess_pile(sample, tokenizer, clip_processor):
    sample = sample[0].decode("utf-8")

    # remove multiple consecutive spaces
    sample = re.sub(r"\s+", " ", sample)
    # remove multiple newlines delimiters
    sample = re.sub(r" +", " ", sample)


    sentences = sent_tokenize(sample)
    # remove sentences that are just punctuation
    sentences = [s for s in sentences if not re.match(r"^\W+$", s)]

    if len(sentences) == 0:
        raise ValueError("No sentences in sample")

    # replace sentences 70% of the time
    indices_replaced = torch.zeros(len(sentences), dtype=torch.bool)
    indices_replaced[torch.rand(len(sentences)) <= 0.7] = True

    if indices_replaced.sum() == 0:
        raise ValueError("No sentences to mask")

    # cap the number of sentences to replace to 10
    if indices_replaced.sum() > 10:
        true_indices = torch.nonzero(indices_replaced).squeeze()
        overflowing = indices_replaced.sum() - 10
        indices_replaced[
            true_indices[torch.randperm(len(true_indices))[:overflowing]]
        ] = False

    chosen_sentences = [
        sentences[i].strip()
        for i in range(len(indices_replaced))
        if indices_replaced[i]
    ]

    for i in range(len(sentences)):
        if indices_replaced[i]:
            sentences[i] = f"<|endofchunk|><image>{sentences[i]}"
    text = " ".join(sentences)
    text = text.replace("<|endofchunk|>", "", 1)
    text = text.replace(" <|endofchunk|>", "<|endofchunk|>")
    text = text.replace("<image> ", "<image>")
    text = text.replace(" <image>", "<image>")


    text = f"{text}<|endofchunk|>{tokenizer.eos_token}"
    tokenizer.padding_side = "right"
    text_tensor = tokenizer(
        text, max_length=256, truncation=True, padding="max_length", return_tensors="pt"
    )

    clip_text_tensor = clip_processor.tokenizer(
        chosen_sentences,
        max_length=24,
        truncation=True,
        padding="max_length",
        return_tensors="pt",
    )

    # pad to 10 sentences
    if len(chosen_sentences) < 10:
        zero_padding = torch.zeros((10 - len(chosen_sentences), 24), dtype=torch.long)
        clip_text_tensor["input_ids"] = torch.cat(
            (clip_text_tensor["input_ids"], zero_padding), dim=0
        )
        clip_text_tensor["attention_mask"] = torch.cat(
            (clip_text_tensor["attention_mask"], zero_padding), dim=0
        )

    return (clip_text_tensor["input_ids"], clip_text_tensor["attention_mask"]), (
        text_tensor["input_ids"],
        text_tensor["attention_mask"],
    )


def get_pile_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    input_shards = args.shards
    assert input_shards is not None
    resampled = getattr(args, "dataset_resampled", False)

    num_samples, num_shards = get_dataset_size(input_shards)
    num_samples = None
    if not num_samples:
        num_samples = args.train_num_samples
        if not num_samples:
            raise RuntimeError(
                "Currently, number of dataset samples must be specified for training dataset. "
                "Please specify via `--train-num-samples` if no dataset length info present."
            )

    # create a shared epoch store to sync epoch to dataloader worker proc
    shared_epoch = SharedEpoch(epoch=epoch)
    if resampled:
        pipeline = [
            ResampledShards2(input_shards, deterministic=True, epoch=shared_epoch)
        ]
    else:
        pipeline = [wds.SimpleShardList(input_shards)]

    preprocess_fn = functools.partial(
        preprocess_pile, clip_processor=image_processor, tokenizer=tokenizer
    )

    # at this point we have an iterator over all the shards
    if not resampled:
        pipeline.extend(
            [
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch,
                ),
                wds.split_by_node,
                wds.split_by_worker,
            ]
        )
    pipeline.extend(
        [
            # at this point, we have an iterator over the shards assigned to each worker at each node
            # wds.tarfile_to_samples(handler=log_and_continue),
            tarfile_to_samples_nothrow,
            wds.shuffle(
                bufsize=_SAMPLE_SHUFFLE_SIZE,
                initial=_SAMPLE_SHUFFLE_INITIAL,
            ),
        ]
    )

    pipeline.extend(
        [
            wds.to_tuple("txt"),
            wds.map(preprocess_fn, handler=log_and_continue),
            wds.batched(args.batch_size, partial=False),
            # wds.map_tuple(preprocess_image_fn, preprocess_text_fn, handler=log_and_continue),
        ]
    )

    dataset = wds.DataPipeline(*pipeline)
    if not resampled:
        assert (
            num_shards >= args.workers * args.world_size
        ), "number of shards must be >= total workers"
    # roll over and repeat a few samples to get same number of full batches on each node
    round_fn = math.floor if floor else math.ceil
    global_batch_size = args.batch_size * args.world_size
    num_batches = round_fn(num_samples / global_batch_size)
    num_workers = max(1, args.workers)
    num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
    num_batches = num_worker_batches * num_workers
    num_samples = num_batches * global_batch_size
    # each worker is iterating over this
    dataset = dataset.with_epoch(num_worker_batches)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=True,
    )

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)


def get_wds_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    input_shards = args.shards
    assert input_shards is not None
    resampled = getattr(args, "dataset_resampled", False)

    num_samples, num_shards = get_dataset_size(input_shards)
    num_samples = None
    if not num_samples:
        num_samples = args.train_num_samples
        if not num_samples:
            raise RuntimeError(
                "Currently, number of dataset samples must be specified for training dataset. "
                "Please specify via `--train-num-samples` if no dataset length info present."
            )

    # create a shared epoch store to sync epoch to dataloader worker proc
    shared_epoch = SharedEpoch(epoch=epoch)
    if resampled:
        pipeline = [
            ResampledShards2(input_shards, deterministic=True, epoch=shared_epoch)
        ]
    else:
        pipeline = [wds.SimpleShardList(input_shards)]

    # create two preprocess functions that take in the passed in image_processor and tokenizer
    preprocess_image_fn = functools.partial(
        preprocess_image, image_processor=image_processor
    )
    preprocess_text_fn = functools.partial(preprocess_text, tokenizer=tokenizer)

    # at this point we have an iterator over all the shards
    if not resampled:
        pipeline.extend(
            [
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch,
                ),
                wds.split_by_node,
                wds.split_by_worker,
            ]
        )
    pipeline.extend(
        [
            # at this point, we have an iterator over the shards assigned to each worker at each node
            # wds.tarfile_to_samples(handler=log_and_continue),
            tarfile_to_samples_nothrow,
            wds.shuffle(
                bufsize=_SAMPLE_SHUFFLE_SIZE,
                initial=_SAMPLE_SHUFFLE_INITIAL,
            ),
        ]
    )

    pipeline.extend(
        [
            wds.select(filter_no_caption_or_no_image),
            wds.decode("pilrgb", handler=log_and_continue),
            wds.to_tuple("jpg;png;jpeg", "txt", handler=log_and_continue),
            wds.batched(args.batch_size, partial=False),
            wds.map_tuple(
                preprocess_image_fn, preprocess_text_fn, handler=log_and_continue
            ),
        ]
    )

    dataset = wds.DataPipeline(*pipeline)
    if not resampled:
        assert (
            num_shards >= args.workers * args.world_size
        ), "number of shards must be >= total workers"
    # roll over and repeat a few samples to get same number of full batches on each node
    round_fn = math.floor if floor else math.ceil
    global_batch_size = args.batch_size * args.world_size
    num_batches = round_fn(num_samples / global_batch_size)
    num_workers = max(1, args.workers)
    num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
    num_batches = num_worker_batches * num_workers
    num_samples = num_batches * global_batch_size
    # each worker is iterating over this
    dataset = dataset.with_epoch(num_worker_batches)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=True,
    )

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)



def remove_consecutive_spaces(sentence):
    sentence = re.sub(r"\s+", " ", sentence)
    return sentence

def remove_multiple_newline_limiters(sentence):
    sentence =  re.sub(r" +", " ", sentence)
    return sentence

def preprocess_sentence(sentence):
    sentence = remove_consecutive_spaces(sentence)
    sentence = remove_multiple_newline_limiters(sentence)
    return sentence

def remove_punctuation_based_sentences(interleaved_list, is_image_list):

    """
    Sets sentences that just punctuations to empty string
    """
    for i, (interleaved_input, is_image) in enumerate(zip(interleaved_list, is_image_list)):
        if not is_image:
            is_punctuation = re.match(r"^\W+$", interleaved_input)
            if is_punctuation:
                interleaved_list[i] =  ""
    return interleaved_list, is_image_list

def filter_no_json_or_no_image(sample):
    has_json = "json" in sample
    has_image = len(dict(filter(lambda item: item[0].endswith("png") or item[0].endswith("jpg") or item[0].endswith("jpeg"), sample.items()))) >= 1
    return has_json and has_image

def get_image(input, images):
    image_name = input.split(".")[-2]+"."+input.split(".")[-1] 
    return images[image_name]

def is_tiny_image(image):
    """
    Removes images that are smaller in size for example RSS icons
    """
    return image.size[0] <= TINY_IMAGE_SIZE_THRESHOLD or image.size[1] <= TINY_IMAGE_SIZE_THRESHOLD

def remove_unwanted_images(interleaved_list, is_image_list, images):
    """
    Smaller images are usually asscoiated with icons/ advertisements and may not be relevant to the input text. 
    To tackle this issue, the method updates the interleaved data and the is_image list if the images are too small
    We also do not want to include more than five images in any interleaved sample so this method takes care of that too. 
    """
    valid_images = 0
    for i, (interleaved_input, is_image) in enumerate(zip(interleaved_list, is_image_list)):
        if is_image:
            interleaved_list[i] = get_image(interleaved_input, images)
            is_unwanted = is_tiny_image(interleaved_list[i])
            if not is_unwanted:
                valid_images +=1

            if is_unwanted or valid_images > MAX_NUM_IMAGES:
                interleaved_list[i] = ""
                is_image_list[i] = 0

    num_images = min(MAX_NUM_IMAGES, valid_images)
    return num_images, interleaved_list, is_image_list


def prepare_text_data(interleaved_list, text_tokenizer):
    """
    The method prepares text tensor
    """
    text = "".join(interleaved_list)
    text = f"{text}<|endofchunk|>{text_tokenizer.eos_token}"
    text_tokenizer.padding_side = "right"
    text_tensor = text_tokenizer(text, max_length=MAX_NUM_TOKENS, truncation=True, padding="max_length", return_tensors="pt")
    return text_tensor


def prepare_image_data(image_list, image_processor):
    images_tensor = preprocess_image(image_list, image_processor) 
    num_images = len(image_list)
    if num_images < MAX_NUM_IMAGES:
        zero_padding = torch.zeros((MAX_NUM_IMAGES - num_images, N_CHANNELS, INTERLEAVED_IMAGE_SIZE, INTERLEAVED_IMAGE_SIZE), dtype=torch.float)
        images_tensor = torch.cat((images_tensor, zero_padding), dim=0)
    return images_tensor


def substitute_with_image_tag(interleaved_list, is_image_list, images):
    """
    The method creates a list of images (PIL) format and updates interleaved_list
    with <image> tags.
    Returns: A list of images and the updated interleaved_list list with samples

    Examples: 
    [<PIL_image>, <PIL_image>, "test sentence"]                                     ---> ["<image>", "<image>", "test sentence"]
    [<PIL_image>, <PIL_image>, "test sentence 1", "test sentence 2"]                ---> ["<image>", "<image>", "test sentence 1<|endofchunk|>", "test sentence 2"]
    [<PIL_image>, <PIL_image>, "test sentence 1", <PIL_image>, "test sentence 2"]   ---> ["<image>", "<image>", "test sentence 1<|endofchunk|>", "<image>", "test sentence 2"]
    """
    images = []
    for i, (interleaved_input, is_image) in enumerate(zip(interleaved_list, is_image_list)):
        if is_image:
            images.append(interleaved_input)
            interleaved_list[i] =  f"<image>"
        else:
            interleaved_list[i] = interleaved_list[i].strip()
            is_previous_image = interleaved_list[i-1] == f"<image>"
            is_last_sentence = i == (len(interleaved_list) - 1)
            if is_previous_image and not is_last_sentence:
                interleaved_list[i] =  f"{interleaved_list[i]}<|endofchunk|>"
            

    assert len(images) > 0, "images should be >= 1"
    return images, interleaved_list, is_image_list


def filter_out_empty_sentences(interleaved_list, is_image_list):
    filtered_interleaved_list = []
    filtered_is_image_list = []
    for i, (interleaved_input, is_image) in enumerate(zip(interleaved_list, is_image_list)):
        if not interleaved_input == "":
            filtered_interleaved_list.append(interleaved_input)
            filtered_is_image_list.append(is_image)
    return filtered_interleaved_list, filtered_is_image_list


def preprocess_sentences(interleaved_list, is_image_list):
    for i, (interleaved_input, is_image) in enumerate(zip(interleaved_list, is_image_list)):
        if not is_image:
            interleaved_list[i] = preprocess_sentence(interleaved_input)
    interleaved_list, is_image_list = remove_punctuation_based_sentences(interleaved_list, is_image_list)
    interleaved_list, is_image_list = filter_out_empty_sentences(interleaved_list, is_image_list)
    assert len(interleaved_list) == len(is_image_list) , "lengths of the interleaved and is_image list should be same"
    return len(interleaved_list), interleaved_list, is_image_list


def preprocess_interleaved_sample(interleaved_list, is_image_list, images, text_tokenizer, image_processor):
    num_images, interleaved_list, is_image_list = remove_unwanted_images(interleaved_list, is_image_list, images)
    if num_images == 0:
        raise ValueError("No images in sample")

    num_sentences, interleaved_list, is_image_list = preprocess_sentences(interleaved_list, is_image_list)
    if num_sentences == 0:
        raise ValueError("No sentences in sample")

    images, interleaved_list, is_image_list = substitute_with_image_tag(interleaved_list, is_image_list, images)

    text_tensor = prepare_text_data(interleaved_list, text_tokenizer)
    images_tensor = prepare_image_data(images, image_processor) 
    return images_tensor, (text_tensor["input_ids"], text_tensor["attention_mask"])



def preprocess_interleaved_data(data, text_tokenizer, image_processor):
    sample = data["json"]
    interleaved_list = sample["interleaved_list"]
    is_image_list = sample["is_image"]

    images = {k: v for k, v  in data.items() if k.endswith("png") or k.endswith("jpg") or k.endswith("jpeg")}
    images_tensor, (text_input_ids, text_attention_mask) = preprocess_interleaved_sample(interleaved_list, is_image_list, images, text_tokenizer, image_processor)
    return images_tensor, (text_input_ids, text_attention_mask)
    

def get_interleaved_dataset(args, image_processor, tokenizer, epoch=0, floor=False):
    input_shards = args.shards
    assert input_shards is not None
    resampled = getattr(args, "dataset_resampled", False)

    num_samples, num_shards = get_dataset_size(input_shards)
    num_samples = None
    if not num_samples:
        num_samples = args.train_num_samples
        if not num_samples:
            raise RuntimeError(
                "Currently, number of dataset samples must be specified for training dataset. "
                "Please specify via `--train-num-samples` if no dataset length info present."
            )

    # create a shared epoch store to sync epoch to dataloader worker proc
    shared_epoch = SharedEpoch(epoch=epoch)
    if resampled:
        pipeline = [
            ResampledShards2(input_shards, deterministic=True, epoch=shared_epoch)
        ]
    else:
        pipeline = [wds.SimpleShardList(input_shards)]

    # create two preprocess functions that take in the passed in image_processor and tokenizer
    preprocess_interleaved_data_fn = functools.partial(preprocess_interleaved_data, text_tokenizer=tokenizer, image_processor = image_processor)


    # at this point we have an iterator over all the shards
    if not resampled:
        pipeline.extend(
            [
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch,
                ),
                wds.split_by_node,
                wds.split_by_worker,
            ]
        )
    pipeline.extend(
        [
            # at this point, we have an iterator over the shards assigned to each worker at each node
            # wds.tarfile_to_samples(handler=log_and_continue),
            tarfile_to_samples_nothrow,
            wds.shuffle(
                bufsize=_SAMPLE_SHUFFLE_SIZE,
                initial=_SAMPLE_SHUFFLE_INITIAL,
            ),
        ]
    )

    pipeline.extend(
        [
            wds.select(filter_no_json_or_no_image),
            wds.decode("pilrgb", handler=log_and_continue),
            wds.map(preprocess_interleaved_data_fn ,handler=log_and_continue),
            wds.batched(args.batch_size, partial=False),
        ]
    )

    dataset = wds.DataPipeline(*pipeline)
    if not resampled:
        assert (
            num_shards >= args.workers * args.world_size
        ), "number of shards must be >= total workers"
    # roll over and repeat a few samples to get same number of full batches on each node
    round_fn = math.floor if floor else math.ceil
    global_batch_size = args.batch_size * args.world_size
    num_batches = round_fn(num_samples / global_batch_size)
    num_workers = max(1, args.workers)
    num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
    num_batches = num_worker_batches * num_workers
    num_samples = num_batches * global_batch_size
    # each worker is iterating over this
    dataset = dataset.with_epoch(num_worker_batches)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=True,
    )

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)



def get_dataset_fn(dataset_type):
    if dataset_type == "image_text":
        return get_wds_dataset
    elif dataset_type == "pile":
        return get_pile_dataset
    elif dataset_type == "interleaved":
        return get_interleaved_dataset
    else:
        raise ValueError(f"Unsupported dataset type: {dataset_type}")


def get_data(args, image_processor, tokenizer, epoch=0):
    return get_dataset_fn(args.dataset_type)(
        args, image_processor=image_processor, epoch=epoch, tokenizer=tokenizer
    )