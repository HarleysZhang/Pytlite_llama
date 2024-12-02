from typing import Optional
import torch, logging
from PIL import Image

from typing import List, Literal, Optional, Tuple, TypedDict, Generator, Union
from .executor.model_executor import ModelExecutor
from .utils.constants import *

from transformers import AutoConfig, AutoProcessor, LlavaForConditionalGeneration

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CompletionPrediction(TypedDict, total=False):
    generation: str
    tokens: List[str]  # not required
    logprobs: List[float]  # not required

def tokenizer_image_token(
    prompt, 
    tokenizer, 
    image_token_index=IMAGE_TOKEN_INDEX, 
    return_tensors=None
):
    """
    处理包含特殊标记 <image> 的文本提示, 将其转换为相应的 token 序列，并在 <image> 位置插入指定的图像 token 索引。
    """
    # 分割并分词：将 prompt 按照 <image> 分割为多个片段，并对每个片段进行分词，生成对应的 input_ids 列表。
    prompt_chunks = [tokenizer(chunk).input_ids for chunk in prompt.split('<image>')]

    def insert_separator(X, sep):
        return [ele for sublist in zip(X, [sep]*len(X)) for ele in sublist][:-1]

    input_ids = []
    offset = 0
    if len(prompt_chunks) > 0 and len(prompt_chunks[0]) > 0 and prompt_chunks[0][0] == tokenizer.bos_token_id:
        offset = 1
        input_ids.append(prompt_chunks[0][0])

    for x in insert_separator(prompt_chunks, [image_token_index] * (offset + 1)):
        input_ids.extend(x[offset:])

    if return_tensors is not None:
        if return_tensors == 'pt':
            return torch.tensor(input_ids, dtype=torch.long)
        raise ValueError(f'Unsupported tensor type: {return_tensors}')
    return input_ids


class LlavaGeneratorStream:
    """
    GenerateText 类用于加载LLaMA模型并执行迭代式生成式推理 (文本生成)。
    """
    def __init__(self, 
        checkpoints_dir: str,
        tokenizer_path: str,
        max_batch_size = 32,
        max_seq_len = 2048,
        load_model = True,
        triton_weight = True,
        compiled_model = True,
        device="cuda",
    ):
        self.checkpoints_dir = checkpoints_dir
        self.compiled_model = compiled_model
        self.max_batch_size = max_batch_size
        self.max_seq_len = max_seq_len

        processor = AutoProcessor.from_pretrained(checkpoints_dir)
        self.image_processor = processor.image_processor

        self.model_executor = ModelExecutor.build(
            checkpoints_dir = checkpoints_dir,
            tokenizer_path = tokenizer_path,
            load_model = load_model,
            max_batch_size =  max_batch_size,
            max_seq_len = max_seq_len,
            triton_weight = triton_weight,
            device = device
        )
        self.tokenizer = self.model_executor.tokenizer
        # self.model_config = self.model_executor.model_config

    def encode_images(self, image_items: List[Union[str, Image.Image]]):
        images = []
        for item in image_items:
            if isinstance(item, Image.Image):
                image = item
            elif item.startswith("http://") or item.startswith("https://"):
                import requests
                image = Image.open(requests.get(item, stream=True).raw)
            else:
                image = Image.open(item)
            images.append(image.convert("RGB"))

        images = self.image_processor.preprocess(images, return_tensors="pt")["pixel_values"]
        print(images)

        return images


    @torch.inference_mode()
    def generate_stream(
        self,
        prompt_tokens: List[List[int]],
        image_tensors: Optional[torch.FloatTensor] = None,
        max_gen_len: int = 2048,
        temperature: float = 0.6,
        top_p: float = 0.9,
        echo: bool = False,
    ) -> Generator[Tuple[List[str], Optional[List[float]]], None, None]:
        """
        基于提供的 prompt_tokens, 使用语言生成模型逐个生成 token, 并在生成时立即输出。

        参数：
            prompt_tokens (List[List[int]]): 已经进行分词的 prompt, 每个 prompt 是一个整数列表。
            max_gen_len (int): 生成的最大长度。
            temperature (float, optional): 控制采样随机性的温度值。默认为 0.6。
            top_p (float, optional): 用于 nucleus sampling 的概率阈值。默认为 0.9。
            logprobs (bool, optional): 是否计算生成 token 的对数概率。默认为 False。
            echo (bool, optional): 是否在输出中包含 prompt_tokens。默认为 False。
            
        generator 输出：
            Tuple[List[str], Optional[List[float]]]: 包含生成的文本和对应的对数概率(如果 logprobs 为 True)。
        说明：
            该方法在生成循环中，每生成一个新 token, 就立即输出对应的文本和概率(如果需要）。
        """
        bsz = len(prompt_tokens)
        assert bsz <= self.max_batch_size, (bsz, self.max_batch_size)

        min_prompt_len = min(len(t) for t in prompt_tokens)
        max_prompt_len = max(len(t) for t in prompt_tokens)
        assert max_prompt_len <= self.max_seq_len
        total_len = min(self.max_seq_len, max_gen_len + max_prompt_len)
        
        pad_id = self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else self.tokenizer.eos_token_id
        tokens = torch.full((bsz, total_len), pad_id, dtype=torch.long, device="cuda")
        input_text_mask = tokens != pad_id
        eos_reached = torch.tensor([False] * bsz, device="cuda")

        for k, t in enumerate(prompt_tokens):
            tokens[k, : len(t)] = t.clone().detach().to(dtype=torch.long, device="cuda")

        prev_pos = 0
        last_yielded_pos = [len(prompt_tokens[i]) if not echo else 0 for i in range(bsz)] # 初始化每个样本已输出的位置

        if min_prompt_len == total_len: # 如果 prompt 已经达到最大长度，无需生成
            logits, _ = self.model.forward(tokens, prev_pos, image_tensors)

        for cur_pos in range(min_prompt_len, total_len):
            input_ids = tokens[:, prev_pos: cur_pos]
            logits, select_index = self.model_executor.forward(input_ids, prev_pos, image_tensors)

            if temperature > 0:
                probs = torch.softmax(logits[:, -1] / temperature, dim=-1)
                next_token = sample_top_p(probs, top_p)
            else:
                next_token = torch.argmax(logits[:, -1], dim=-1)

            next_token = next_token.reshape(-1)  # shape is (batch_size,)

            # 仅在需要生成的情况下替换 token
            next_token = torch.where(input_text_mask[:, cur_pos], tokens[:, cur_pos], next_token)
            tokens[:, cur_pos] = next_token

            eos_reached |= (~input_text_mask[:, cur_pos]) & (next_token == self.tokenizer.eos_token_id)
            prev_pos = cur_pos
            
            # 为整个批次收集输出
            batch_outputs = []
            for i in range(bsz):
                start = last_yielded_pos[i]
                end = cur_pos + 1
                if start < end:
                    token = tokens[i, start:end].tolist()
                    text = self.tokenizer.decode(token)
                    batch_outputs.append(text)
                    last_yielded_pos[i] = end
                else:
                    batch_outputs.append('') # 如果没有新生成的内容，添加空字符串

            # 将整个批次的输出一次性 yield
            yield batch_outputs

            if eos_reached.all():
                break
        
        # 减少 kv cache 内存管理器的引用计数
        self.model_executor.kv_mem_manager.release_ref(select_index)

    def text_completion_stream(
        self,
        prompts: List[str],
        image_items: List[Union[str, Image.Image]],
        temperature: float = 0.6,
        top_p: float = 0.9,
        max_gen_len: Optional[int] = None,
        echo: bool = False,
    ) -> Generator[List[CompletionPrediction], None, None]:
        
        if max_gen_len is None:
            max_gen_len = self.max_seq_len - 1

        prompts = "USER: <image>\nWhat's the content of the image? ASSISTANT:"
        image_items = ["https://www.ilankelman.org/stopsigns/australia.jpg"]

        # prompt_tokens = [self.tokenizer.encode(x, add_special_tokens=True) for x in prompts]
        prompt_tokens = (tokenizer_image_token(prompts, self.tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt").unsqueeze(0).cuda())
        image_tensors = self.encode_images(image_items).cuda()

        stream = self.generate_stream(
            prompt_tokens=prompt_tokens,
            image_tensors=image_tensors,
            max_gen_len=max_gen_len,
            temperature=temperature,
            top_p=top_p,
            echo=echo,
        )

        # 初始化每个样本的生成结果
        completions = [{'generation': '', 'tokens': []} for _ in prompts]
        for batch_outputs in stream:
            for i, text in enumerate(batch_outputs):
                completions[i]['generation'] += text
            yield completions.copy()
    
def sample_top_p(probs, p):
    """
    执行 Top-p (Nucleus) 采样, 从概率分布中采样下一个词。
    
    参数：
        probs (torch.Tensor): 概率分布张量，形状为 `[batch_size, vocab_size]`。
        p (float): 累积概率阈值，取值范围在 0 到 1 之间。
    返回：
        torch.Tensor: 采样得到的词索引，形状为 `[batch_size, 1]`。

    说明：
        Top-p 采样算法: 选择概率累积和超过阈值 p 的最小集合，将这些词的概率重新归一化后进行采样。
    """
    # 对概率分布进行降序排序。probs_sort: 排序后的概率值，形状与 probs 相同。probs_idx: 排序后的索引，用于映射回原始词汇表。
    probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)
    # 计算排序后概率的累积和. 返回的 probs_sum 是累积概率分布。
    probs_sum = torch.cumsum(probs_sort, dim=-1)
    # 保留累积概率未超过阈值 p 的词汇的概率，其余词汇的概率被置为 0.0。
    mask = probs_sum - probs_sort > p # 创建掩码，对于每个位置，计算累积概率（不包括当前词）是否超过阈值 p。
    probs_sort[mask] = 0.0 # 将累积概率超过阈值 p 的词的概率置零。

    # 对剩余的概率重新归一化, 确保总和为 1。
    probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
    # 从重新归一化的概率分布中采样下一个词. 返回的 next_token 是采样得到的词在排序后概率分布中的索引。
    next_token_sorted_idx = torch.multinomial(probs_sort, num_samples=1)
    # 在 probs_idx 的最后一维（dim=-1）中，使用 next_token_sorted_idx 作为索引，提取对应的值。沿着 dim=1（列）进行索引提取
    # NOTE: torch.gather 函数按照给定的索引张量 index，从输入张量中收集 (获取) 数据，并返回一个与索引张量形状一致的张量。
    next_token = torch.gather(probs_idx, -1, index = next_token_sorted_idx)
    
    return next_token # 返回采样得到的下一个词的索引