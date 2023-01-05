#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
=================================================
@Project ：span-aste
@IDE     ：PyCharm
@Author  ：Mr. Wireless
@Date    ：2022/1/18 16:19 
@Desc    ：
==================================================
"""
import argparse
import os
import random
import time
import torch
from torch.utils.data import DataLoader

from evaluate import evaluate
from models.losses import log_likelihood
from models.metrics import SpanEvaluator
from utils.bar import ProgressBar
from utils.dataset import CustomDataset
from models.collate import collate_fn, gold_labels
import numpy as np
from models.model import SpanAsteModel
from utils.processor import Res15DataProcessor
from utils.tager import SpanLabel
from utils.tager import RelationLabel
from transformers import BertTokenizer, BertModel, get_linear_schedule_with_warmup
from tqdm import trange
from matplotlib import pyplot as plt
from thop import profile

device = "cuda" if torch.cuda.is_available() else "cpu"
if torch.cuda.is_available():
    torch.cuda.empty_cache()
print(f"using device:{device}")


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)


def do_train():
    # set seed
    set_seed(args.seed)

    # tokenizer
    tokenizer = BertTokenizer.from_pretrained(args.bert_model)

    # create processor
    processor = Res15DataProcessor(tokenizer, args.max_seq_len)

    print("Loading Train & Eval Dataset...")
    # Load dataset
    train_dataset = CustomDataset("train", args.train_path, processor, tokenizer, args.max_seq_len)
    eval_dataset = CustomDataset("dev", args.dev_path, processor, tokenizer, args.max_seq_len)

    print("Construct Dataloader...")
    train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True, collate_fn=collate_fn)
    eval_dataloader = DataLoader(eval_dataset, batch_size=args.batch_size, shuffle=True, collate_fn=collate_fn)

    print("Building SPAN-ASTE model...")
    # get dimension of target and relation
    target_dim, relation_dim = len(SpanLabel), len(RelationLabel)
    # build span-aste model
    model = SpanAsteModel(
        args.bert_model,
        target_dim,
        relation_dim,
        device=device
    )

    model.to(device)

    no_decay = ['bias', 'LayerNorm.weight']
    bert_param_optimizer = list(model.bert.named_parameters())
    span_linear_param_optimizer = list(model.span_ffnn.named_parameters())
    pair_linear_param_optimizer = list(model.pairs_ffnn.named_parameters())
    optimizer_grouped_parameters = [
        {'params': [p for n, p in bert_param_optimizer if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay, 'lr': args.learning_rate},
        {'params': [p for n, p in bert_param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0,
         'lr': args.learning_rate},

        {'params': [p for n, p in span_linear_param_optimizer if not any(nd in n for nd in no_decay)],
         'weight_decay': 0.0, 'lr': 1e-3},
        {'params': [p for n, p in span_linear_param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0,
         'lr': 1e-3},

        {'params': [p for n, p in pair_linear_param_optimizer if not any(nd in n for nd in no_decay)],
         'weight_decay': 0.0, 'lr': 1e-3},
        {'params': [p for n, p in pair_linear_param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0,
         'lr': 1e-3}
    ]

    print("Building Optimizer...")
    optimizer = torch.optim.AdamW(optimizer_grouped_parameters, lr=args.learning_rate)
    num_training_steps = len(train_dataloader) * args.num_epochs
    num_warmup_steps = num_training_steps * args.warmup_proportion
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=num_warmup_steps,
                                                num_training_steps=num_training_steps)

    metric = SpanEvaluator()

    tic_train = time.time()
    global_step = 0
    best_f1 = 0
    loss_list = []

    plot_loss_list = []; precision_list = []; recall_list = []; f1_list = []
    for epoch in trange(1, args.num_epochs + 1):
        # pbar = ProgressBar(n_total=len(train_dataloader), desc='Training')
        model.train()
        for batch_ix, batch in enumerate(train_dataloader):
            input_ids, attention_mask, token_type_ids, spans, relations, span_labels, relation_labels, seq_len = batch
            input_ids = torch.tensor(input_ids, device=device)
            attention_mask = torch.tensor(attention_mask, device=device)
            token_type_ids = torch.tensor(token_type_ids, device=device)

            # FLOPs, parameters
            if epoch == 1 and batch_ix == 0:
                flops, params = profile(model, inputs=(input_ids, attention_mask, token_type_ids, seq_len))
                print(f'FLOPs: {flops/1000**3:.2f}G')
                print(f'Parameters: {params/1000**2:.2f}M')

            # forward
            spans_probability, span_indices, relations_probability, candidate_indices = model(
                input_ids, attention_mask, token_type_ids, seq_len)
            
            gold_span_indices, gold_span_labels = gold_labels(span_indices, spans, span_labels)
            loss_ner = log_likelihood(spans_probability, span_indices, gold_span_indices, gold_span_labels)

            gold_relation_indices, gold_relation_labels = gold_labels(candidate_indices, relations, relation_labels)
            loss_relation = log_likelihood(relations_probability, candidate_indices, gold_relation_indices,
                                           gold_relation_labels)

            # loss compute
            loss = 0.2 * loss_ner + loss_relation

            # backward
            optimizer.zero_grad()

            loss.backward()
            optimizer.step()
            scheduler.step()

            loss_list.append(float(loss))
            # pbar(batch_ix, {"loss": float(loss)})
            # print("")
            global_step += 1
            if global_step % args.logging_steps == 0:
                time_diff = time.time() - tic_train
                loss_avg = sum(loss_list) / len(loss_list)
                plot_loss_list.append(loss_avg)

                print(
                    "global step %d, epoch: %d, loss: %.5f, speed: %.2f step/s"
                    % (global_step, epoch, loss_avg,
                       args.logging_steps / time_diff))
                tic_train = time.time()

            # valid
            if global_step % args.valid_steps == 0:
                # ckpt_dir = os.path.join(args.ckpt_dir, "model_%d" % global_step)
                # if not os.path.exists(ckpt_dir):
                #     os.makedirs(ckpt_dir)
                # torch.save(model.state_dict(), os.path.join(ckpt_dir, "model.pt"))
                precision, recall, f1 = evaluate(model, metric, eval_dataloader, device)

                precision_list.append(precision)
                recall_list.append(recall)
                f1_list.append(f1)

                print(
                    "Evaluation precision: %.5f, recall: %.5f, F1: %.5f" %
                    (precision, recall, f1))

                if f1 > best_f1:
                    print(
                        f"best F1 performence has been updated: {best_f1:.5f} --> {f1:.5f}"
                    )
                    best_f1 = f1
                    ckpt_dir = os.path.join(args.ckpt_dir)
                    if not os.path.exists(ckpt_dir):
                        os.makedirs(ckpt_dir)
                    torch.save(model.state_dict(), os.path.join(ckpt_dir, "model.pt"))

                tic_train = time.time()

    output_dir = os.path.join(args.output_dir, 'img')
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Plot loss curve
    plt.plot(range(1, len(plot_loss_list)+1), plot_loss_list)
    plt.xlabel('Epoch(s)')
    plt.ylabel('Loss')
    plt.savefig(f'{output_dir}/loss.png')
    plt.clf()

    # Plot accuracy curve
    plt.title('Accuracy')
    plt.ylabel("Accuracy(%)") # y label
    plt.xlabel("Epoch(s)") # x label
    plt.plot(range(1, len(precision_list)+1), [item * 100 for item in precision_list], color='red', linestyle="-", linewidth="1", markersize="8", marker=".", label="Precision")
    plt.plot(range(1, len(recall_list)+1), [item * 100 for item in recall_list], color='blue', linestyle="-", linewidth="1", markersize="8", marker=".", label="Recall")
    plt.plot(range(1, len(f1_list)+1), [item * 100 for item in f1_list], color='green', linestyle="-", linewidth="1", markersize="8", marker=".", label="F1-score")
    plt.minorticks_on()
    plt.legend()
    plt.savefig(f'{output_dir}/accuracy.png')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument("--bert_model", default="bert-base-uncased", type=str,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--batch_size", default=1, type=int, help="Batch size per GPU/CPU for training.")
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for AdamW.")
    parser.add_argument("--weight_decay", default=1e-2, type=float, help="The initial learning rate for AdamW.")
    parser.add_argument("--warmup_proportion", default=0.1, type=float, help="The initial learning rate for AdamW.")
    parser.add_argument("--train_path", default="preprocessed_data", type=str, help="The path of train set.")
    parser.add_argument("--dev_path", default="preprocessed_data", type=str, help="The path of dev set.")
    parser.add_argument("--ckpt_dir", default='./checkpoint', type=str,
                        help="The output directory where the model checkpoints will be written.")
    parser.add_argument("--output_dir", default='./output', type=str,
                        help="The output directory where output learning curve will be written.")
    parser.add_argument("--max_seq_len", default=256, type=int,
                        help="The maximum input sequence length. Sequences longer than this will be split automatically.")
    parser.add_argument("--num_epochs", default=70, type=int, help="Total number of training epochs to perform.")
    parser.add_argument("--seed", default=2022, type=int, help="Random seed for initialization")
    parser.add_argument("--logging_steps", default=480, type=int, help="The interval steps to logging.")
    parser.add_argument("--valid_steps", default=480, type=int,
                        help="The interval steps to evaluate model performance.")
    parser.add_argument("--init_from_ckpt", default=None, type=str,
                        help="The path of model parameters for initialization.")

    args = parser.parse_args()

    do_train()
