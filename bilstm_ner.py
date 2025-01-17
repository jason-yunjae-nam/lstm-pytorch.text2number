import json
import time

from requests import post

import torch
import torch.nn as nn
import torch.optim as optim
import torch.autograd as autograd
import torch.multiprocessing as mp

DATA_PATH = "./data/training_data/training_data.json"
MODEL_PATH = "./data/model/ner/"
DICT_PATH = "./resource/vocab.json"

START_TAG = "<START>"
STOP_TAG = "<STOP>"
EMBEDDING_DIM = 5
HIDDEN_DIM = 4

torch.manual_seed(1)

def argmax(vec):
    # return the argmax as a python int
    _, idx = torch.max(vec, 1)
    return idx.item()


def prepare_sequence(seq, to_ix):
    idxs = [to_ix[w] for w in seq]
    return torch.tensor(idxs, dtype=torch.long)


# Compute log sum exp in a numerically stable way for the forward algorithm
def log_sum_exp(vec):
    max_score = vec[0, argmax(vec)]
    max_score_broadcast = max_score.view(1, -1).expand(1, vec.size()[1])
    return max_score + \
        torch.log(torch.sum(torch.exp(vec - max_score_broadcast)))

class BiLSTM_CRF(nn.Module):

    def __init__(self, vocab_size, tag_to_ix, embedding_dim, hidden_dim):
        super(BiLSTM_CRF, self).__init__()
        self.embedding_dim = embedding_dim
        self.hidden_dim = hidden_dim
        self.vocab_size = vocab_size
        self.tag_to_ix = tag_to_ix
        self.tagset_size = len(tag_to_ix)

        self.word_embeds = nn.Embedding(vocab_size, embedding_dim)
        self.lstm = nn.LSTM(
            embedding_dim, 
            hidden_dim // 2,
            num_layers=1, 
            bidirectional=True
        )

        # Maps the output of the LSTM into tag space.
        self.hidden2tag = nn.Linear(hidden_dim, self.tagset_size)

        # Matrix of transition parameters.  Entry i,j is the score of
        # transitioning *to* i *from* j.
        self.transitions = nn.Parameter(
            torch.randn(self.tagset_size, self.tagset_size))

        # These two statements enforce the constraint that we never transfer
        # to the start tag and we never transfer from the stop tag
        self.transitions.data[tag_to_ix[START_TAG], :] = -10000
        self.transitions.data[:, tag_to_ix[STOP_TAG]] = -10000

        self.hidden = self.init_hidden()

    def init_hidden(self):
        return (
            torch.randn(2, 1, self.hidden_dim // 2),
            torch.randn(2, 1, self.hidden_dim // 2)
        )

    def _forward_alg(self, feats):
        # Do the forward algorithm to compute the partition function
        init_alphas = torch.full((1, self.tagset_size), -10000.)
        # START_TAG has all of the score.
        init_alphas[0][self.tag_to_ix[START_TAG]] = 0.

        # Wrap in a variable so that we will get automatic backprop
        forward_var = init_alphas

        # Iterate through the sentence
        for feat in feats:
            alphas_t = []  # The forward tensors at this timestep
            for next_tag in range(self.tagset_size):
                # broadcast the emission score: it is the same regardless of
                # the previous tag
                emit_score = feat[next_tag].view(
                    1, -1).expand(1, self.tagset_size)
                # the ith entry of trans_score is the score of transitioning to
                # next_tag from i
                trans_score = self.transitions[next_tag].view(1, -1)
                # The ith entry of next_tag_var is the value for the
                # edge (i -> next_tag) before we do log-sum-exp
                next_tag_var = forward_var + trans_score + emit_score
                # The forward variable for this tag is log-sum-exp of all the
                # scores.
                alphas_t.append(log_sum_exp(next_tag_var).view(1))
            forward_var = torch.cat(alphas_t).view(1, -1)
        terminal_var = forward_var + self.transitions[self.tag_to_ix[STOP_TAG]]
        alpha = log_sum_exp(terminal_var)
        return alpha

    def _get_lstm_features(self, sentence):
        self.hidden = self.init_hidden()
        embeds = self.word_embeds(sentence).view(len(sentence), 1, -1)
        lstm_out, self.hidden = self.lstm(embeds, self.hidden)
        lstm_out = lstm_out.view(len(sentence), self.hidden_dim)
        lstm_feats = self.hidden2tag(lstm_out)
        return lstm_feats

    def _score_sentence(self, feats, tags):
        # Gives the score of a provided tag sequence
        score = torch.zeros(1)
        tags = torch.cat([torch.tensor([self.tag_to_ix[START_TAG]], dtype=torch.long), tags])
        for i, feat in enumerate(feats):
            score = score + \
                self.transitions[tags[i + 1], tags[i]] + feat[tags[i + 1]]
        score = score + self.transitions[self.tag_to_ix[STOP_TAG], tags[-1]]
        return score

    def _viterbi_decode(self, feats):
        backpointers = []

        # Initialize the viterbi variables in log space
        init_vvars = torch.full((1, self.tagset_size), -10000.)
        init_vvars[0][self.tag_to_ix[START_TAG]] = 0

        # forward_var at step i holds the viterbi variables for step i-1
        forward_var = init_vvars
        for feat in feats:
            bptrs_t = []  # holds the backpointers for this step
            viterbivars_t = []  # holds the viterbi variables for this step

            for next_tag in range(self.tagset_size):
                # next_tag_var[i] holds the viterbi variable for tag i at the
                # previous step, plus the score of transitioning
                # from tag i to next_tag.
                # We don't include the emission scores here because the max
                # does not depend on them (we add them in below)
                next_tag_var = forward_var + self.transitions[next_tag]
                best_tag_id = argmax(next_tag_var)
                bptrs_t.append(best_tag_id)
                viterbivars_t.append(next_tag_var[0][best_tag_id].view(1))
            # Now add in the emission scores, and assign forward_var to the set
            # of viterbi variables we just computed
            forward_var = (torch.cat(viterbivars_t) + feat).view(1, -1)
            backpointers.append(bptrs_t)

        # Transition to STOP_TAG
        terminal_var = forward_var + self.transitions[self.tag_to_ix[STOP_TAG]]
        best_tag_id = argmax(terminal_var)
        path_score = terminal_var[0][best_tag_id]

        # Follow the back pointers to decode the best path.
        best_path = [best_tag_id]
        for bptrs_t in reversed(backpointers):
            best_tag_id = bptrs_t[best_tag_id]
            best_path.append(best_tag_id)
        # Pop off the start tag (we dont want to return that to the caller)
        start = best_path.pop()
        assert start == self.tag_to_ix[START_TAG]  # Sanity check
        best_path.reverse()
        return path_score, best_path

    def neg_log_likelihood(self, sentence, tags):
        feats = self._get_lstm_features(sentence)
        forward_score = self._forward_alg(feats)
        gold_score = self._score_sentence(feats, tags)
        return forward_score - gold_score

    def forward(self, sentence):  # dont confuse this with _forward_alg above.
        # Get the emission scores from the BiLSTM
        lstm_feats = self._get_lstm_features(sentence)

        # Find the best path, given the features.
        score, tag_seq = self._viterbi_decode(lstm_feats)
        return score, tag_seq

def dict_load(training_data):
    # Load vocab dictionary from vocab.json file.
    with open(DICT_PATH, encoding="utf-8") as file:
        word_to_ix = json.load(file)

    # Account for Hangeul letters not present in vocab dictionary.
    for sentence, tags in training_data:
        for word in sentence:
            if word not in word_to_ix:
                word_to_ix[word] = len(word_to_ix)
                print(word, "not in dict")
    return word_to_ix

def model_load():
    # Load pre-trained model from model.pt file.
    model = torch.load(MODEL_PATH + "model.pt")
    model.eval()
    return model

def train(model, training_data, word_to_ix, tag_to_ix):
    # model = BiLSTM_CRF(len(word_to_ix), tag_to_ix, EMBEDDING_DIM, HIDDEN_DIM)
    optimizer = optim.SGD(model.parameters(), lr=0.01, weight_decay=1e-4)

    # Make sure prepare_sequence from earlier in the LSTM section is loaded
    for epoch in range(15):  # again, normally you would NOT do 300 epochs, it is toy data
        for sentence, tags in training_data:
            # Step 1. Remember that Pytorch accumulates gradients.
            # We need to clear them out before each instance
            model.zero_grad()

            # Step 2. Get our inputs ready for the network, that is,
            # turn them into Tensors of word indices.
            sentence_in = prepare_sequence(sentence, word_to_ix)
            targets = torch.tensor([tag_to_ix[t] for t in tags], dtype=torch.long)

            # Step 3. Run our forward pass.
            loss = model.neg_log_likelihood(sentence_in, targets)

            # Step 4. Compute the loss, gradients, and update the parameters by
            # calling optimizer.step()
            loss.backward()
            optimizer.step()

    # torch.save(model, MODEL_PATH + 'model.pt')

def main():
    # Load training data from training_data.json file.
    with open(DATA_PATH, encoding="utf8") as file:
        training_data = json.load(file)

    """
    # Make up some training data
    >>> training_data = [(
            list("우리 집 주소는 삼십육번지이다."),
            # "O O S O S O O O S B I I O O O O O".split(),
            "O O O O O O O O O B I I O O O O O".split()
        ), (
            list("나는 이월에 죽었어."),
            # "O O S B I O S O O O O".split(),
            "O O O B I O O O O O O".split()
        )]
    """

    # Get Hangeul vocab dictionary
    word_to_ix = dict_load(training_data=training_data)

    # Define tags for named entities.
    tag_to_ix = {
        "B": 0, # Begin tag
        "I": 1, # Inside tag
        "O": 2, # Outside tag
        # "S": 5, # Space
        START_TAG: 3, 
        STOP_TAG: 4
    }

    """
    # Check predictions before training
    >>> with torch.no_grad():
    >>>     precheck_sent = prepare_sequence(training_data[0][0], word_to_ix)
    >>>     precheck_tags = torch.tensor([tag_to_ix[t] for t in training_data[0][1]], dtype=torch.long)
    >>>     print("Predictions before training: ", model(precheck_sent))
    """

    try:
        # Try to load pre-trained model.
        model = model_load() 
        print("model.pt found.")

    except:
        # If pre-trained model is not found, train BiLSTM_CRF model with multithreading processes.
        print("model.pt not found. Training.") 
        
        num_processes = 4 # Pre-defined multithread count.

        k, m = divmod(len(training_data), num_processes)
        fragmented_training_data = list(training_data[i*k+min(i, m):(i+1)*k+min(i+1, m)] for i in range(num_processes))

        model = BiLSTM_CRF(len(word_to_ix), tag_to_ix, EMBEDDING_DIM, HIDDEN_DIM)
        # NOTE: this is required for the ``fork`` method to work
        model.share_memory()
        processes = []

        for rank in range(num_processes):
            p = mp.Process(target=train, args=(model, fragmented_training_data[rank], word_to_ix, tag_to_ix))
            p.start()
            processes.append(p)
        for p in processes:
            p.join()

        # Save trained model as model.pt.
        torch.save(model, MODEL_PATH + 'model.pt')

        # Load trained model from model.pt.
        model = model_load()

        """
        # Training method for BiLSTM_CRF model.
        >>> train(training_data=training_data, word_to_ix=word_to_ix, tag_to_ix=tag_to_ix)
        """

    # Check trained data predictions.
    for sentence, tags in training_data:
        with torch.no_grad():
            precheck_sent = prepare_sequence(sentence, word_to_ix)
            postcheck_tags = model(precheck_sent)[1]
            infer_sentence = ""
            for i, c in enumerate(sentence):
                if postcheck_tags[i] == 0:
                    infer_sentence = infer_sentence + "[" + c
                elif not i == 0 and postcheck_tags[i] == 2 and postcheck_tags[i-1] == 1:
                    infer_sentence = infer_sentence + "]" + c
                else:
                    infer_sentence = infer_sentence + c
                    
            print("Predictions after training: ", infer_sentence)

    # Test data for inference.
    test_data = [
        list("넌 누구니? 나는 이천년 일월 일일에 태어난 사람이야."),
        list("사과 다섯개 먹고싶어 그런데 그냥 여섯 개 먹었다."),
        list("이 이전에 저한테 배당되기 이전에 센터장하고 철인 삼 종 협회장하고 회의를 통해서"),
        list("이런 인권위라든지 또 피해자 분리라든지 이런 부분에 협의가 있었던 걸로 알고 있습니다."),
    ]

    # Check non trained data predictions.
    for sent in test_data:
        start_time = time.time()
        with torch.no_grad():
            precheck_sent = prepare_sequence(sent, word_to_ix)
            postcheck_tag = model(precheck_sent)[1]
            infer_sent = ""
            for i, c in enumerate(sent):
                if postcheck_tag[i] == 0:
                    infer_sent = infer_sent + "[" + c
                elif not i == 0 and postcheck_tag[i] == 2 and postcheck_tag[i-1] == 1:
                    infer_sent = infer_sent + "]" + c
                else:
                    infer_sent = infer_sent + c
            print("--- %s seconds ---" % (time.time() - start_time))
            print("Predictions after training: ", infer_sent, postcheck_tag)

if __name__ == "__main__":
    main()

    """
    import concurrent.futures

    with open(DATA_PATH, encoding="utf8") as file:
        training_data = json.load(file)

    k, m = divmod(len(training_data), 5)
    training_data = list(training_data[i*k+min(i, m):(i+1)*k+min(i+1, m)] for i in range(5))

    start = time.perf_counter()
    threads = []
    for i in range(10):
        t = threading.Thread(target=main, args=(training_data[i]))
        t.start()
        threads.append(t)
        
    for thread in threads:
        thread.join()
    finish = time.perf_counter()
 
    print(f'Finished in {round(finish-start, 2)} second(s)')


    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        # Start the load operations and mark each future with its URL
        future_to_main = {executor.submit(main, td): td for td in training_data}
        for future in concurrent.futures.as_completed(future_to_main):
            future_to_main[future]
            try:
                data = future.result()
            except Exception as exc:
                print('%r generated an exception: %s' % (exc))
    """
