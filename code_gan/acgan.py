import numpy as np
import tensorflow as tf
import os
import time
import h5py
from generator_ac import generator
from discriminator_ac import discriminator
import param_names
import utils
import acgan_model
import argparse
from sklearn import metrics
from scipy.ndimage.interpolation import shift
import sys

prediction_folder = '../predictions'
prediction_words_file = '/predictions_words_acgan'
summary_file = '/home/logan/tmp'
dataset_file = '../data/annotated_dataset.h5'
dictionary_file = '../data/words.dict'
# train_variables_file = '../models/tf_enc_dec_variables.npz'
train_variables_file = '../models/tf_lm_variables.npz'
ckpt_dir = '../models/acgan_ckpts'
vague_terms_file = '../data/vague_terms'
use_checkpoint = False
num_folds = 5

start_time = time.time()

FLAGS = tf.app.flags.FLAGS
tf.app.flags.DEFINE_integer('EPOCHS', 50,
                            'Num epochs.')
tf.app.flags.DEFINE_integer('VOCAB_SIZE', 5000,
                            'Number of words in the vocabulary.')
tf.app.flags.DEFINE_integer('LATENT_SIZE', 512,
                            'Size of both the hidden state of RNN and random vector z.')
tf.app.flags.DEFINE_integer('SEQUENCE_LEN', 50,
                            'Max length for each sentence.')
tf.app.flags.DEFINE_integer('EMBEDDING_SIZE', 300,
                            'Max length for each sentence.')
tf.app.flags.DEFINE_integer('PATIENCE', 200,
                            'Max length for each sentence.')
tf.app.flags.DEFINE_integer('BATCH_SIZE', 64,
                            'Max length for each sentence.')
tf.app.flags.DEFINE_integer('NUM_CLASSES', 4,
                            'Max length for each sentence.')
tf.app.flags.DEFINE_integer('CLASS_EMBEDDING_SIZE', 1,
                            'Max length for each sentence.')
tf.app.flags.DEFINE_string('CELL_TYPE', 'GRU',
                            'Which RNN cell for the RNNs.')
tf.app.flags.DEFINE_string('MODE', 'TRAIN',
                            'Whether to run in train or test mode.')
tf.app.flags.DEFINE_boolean('SAMPLE', True,
                            'Whether to sample from the generator distribution to get fake samples.')
tf.set_random_seed(123)
np.random.seed(123)
'''
--------------------------------

LOAD DATA

--------------------------------
'''
# Store model using sampling in a different location
if FLAGS.SAMPLE:
    ckpt_dir = '../models/acgan_sample_ckpts'
    gan_variables_file = ckpt_dir + '/tf_acgan_variables_'
    
# Make directories for model files and prediction files
if not os.path.exists(ckpt_dir):
    os.makedirs(ckpt_dir)
if not os.path.exists(prediction_folder):
    os.makedirs(prediction_folder)
for fold_num in range(num_folds):
    fold_ckpt_dir = ckpt_dir + '/' + str(fold_num)
    if not os.path.exists(fold_ckpt_dir):
        os.makedirs(fold_ckpt_dir)
    fold_prediction_dir = prediction_folder + '/' + str(fold_num)
    if not os.path.exists(fold_prediction_dir):
        os.makedirs(fold_prediction_dir)

print('loading model parameters')
params = np.load(train_variables_file)
params_dict = {}
for key in params.keys():
    params_dict[key] = params[key]
params.close()
params = params_dict
# Make padding symbol's embedding = 0
pretrained_embedding_matrix = params[param_names.GAN_PARAMS.EMBEDDING[0]]
pretrained_embedding_matrix[0] = np.zeros(pretrained_embedding_matrix[0].shape)
params[param_names.GAN_PARAMS.EMBEDDING[0]] = pretrained_embedding_matrix

print('loading dictionary')
d = {}
word_to_id = {}
with open(dictionary_file) as f:
    for line in f:
       (word, id) = line.split()
       d[int(id)] = word
       word_to_id[word] = int(id)
       
print('loading vague terms vector')
vague_terms = np.zeros((FLAGS.VOCAB_SIZE))
with open(vague_terms_file) as f:
    for line in f:
        words = line.split()
        if not len(words) == 1:
            print('excluded', words, 'because it is not 1 word:')
            continue
        word = words[0]
        if not word_to_id.has_key(word):
            print(word, 'is not in dictionary')
            continue
        id = word_to_id[word]
        if id >= vague_terms.shape[0]:
            print(word, 'is out of vocabulary')
            continue
        vague_terms[id] = 1
    
def load_train_test_data(fold_num=0):
    print('loading training and test data')
    with h5py.File(dataset_file, 'r') as data_file:
        fold = data_file['fold1']
        train_x = fold['train_X'][:]
        train_y = fold['train_Y_sentence'][:]
        test_x = fold['test_X'][:]
        test_y = fold['test_Y_sentence'][:]
    print 'Number of training instances: ' + str(train_y.shape[0])
    # Remove </s> symbols
    train_x[train_x == 3] = 0
    test_x[test_x == 3] = 0
    # Shift over to remove <s> symbols
    train_x = shift(train_x, [0,-1], cval=0)
    test_x = shift(test_x, [0,-1], cval=0)
            
#     print train_x
#     for i in range(min(5, len(train_x))):
#         for j in range(len(train_x[i])):
#             if train_x[i][j] == 0:
#                 continue
#             word = d[train_x[i][j]]
#             print word + ' ',
#         print '(' + str(train_y[i]) + ')\n'
    return train_x, train_y, test_x, test_y

def batch_generator(x, y, batch_size=FLAGS.BATCH_SIZE):
    data_len = x.shape[0]
    for i in range(0, data_len, batch_size):
        x_batch = x[i:min(i+batch_size,data_len)]
        # If giving the discriminator the vocab distribution, then we need to use a 1-hot representation
        if not FLAGS.SAMPLE:
            x_batch_transpose = np.transpose(x_batch)
            x_batch_one_hot = np.eye(FLAGS.VOCAB_SIZE)[x_batch_transpose.astype(int)]
            x_batch_one_hot_reshaped = x_batch_one_hot.reshape([-1,FLAGS.SEQUENCE_LEN,FLAGS.VOCAB_SIZE])
        y_batch = y[i:min(i+batch_size,data_len)]
        if not FLAGS.SAMPLE:
            yield x_batch_one_hot_reshaped, y_batch, i, data_len
        else:
            yield x_batch, y_batch, i, data_len

'''
--------------------------------

MAIN

--------------------------------
'''

def save_samples_to_file(generated_sequences, batch_fake_c, fold_num, epoch):
    fold_prediction_dir = prediction_folder + '/' + str(fold_num)
    file_name = fold_prediction_dir + prediction_words_file + '_epoch_' + str(epoch)
    with open(file_name, 'w') as f:
        for i in range(len(generated_sequences)):
            for j in range(len(generated_sequences[i])):
                if generated_sequences[i][j] == 0:
                    f.write('<UNK> ')
                else:
                    word = d[generated_sequences[i][j]]
                    f.write(word + ' ')
            f.write('(' + str(batch_fake_c[i]) + ')\n\n')

def print_metrics(y_true, y_pred):
    print 'Performance Metrics\n-------------------\n'
    print ('Accuracy', metrics.accuracy_score(y_true, y_pred))
    print ''
    report = metrics.classification_report(y_true,y_pred)
    print report + '\n'
    confusion_matrix = metrics.confusion_matrix(y_true, y_pred)
    print 'Confusion Matrix\n-------------------\n'
    print '\t\t',
    for i in range(len(confusion_matrix)):
        print str(i) + '\t',
    print '\n'
    for i in range(len(confusion_matrix)):
        print str(i) + '\t\t',
        for j in range(len(confusion_matrix[i])):
            print str(confusion_matrix[i,j]) + '\t',
        print ''
            
def sample_Z(m, n):
    return np.zeros((m, n))
#     return np.random.normal(size=[m, n])

def sample_C(m):
    return np.random.randint(low=0, high=FLAGS.NUM_CLASSES, size=m)
    
def train(model, train_x, train_y, fold_num):
    print 'building graph'
    if not model.is_built:
        model.build_graph(include_optimizer=True)
    print 'training'
    with tf.Session() as sess:
        train_writer = tf.summary.FileWriter(summary_file + '/train', sess.graph)
        tf.global_variables_initializer().run()
        model.assign_variables(sess)
        min_test_cost = np.inf
        num_mistakes = 0
        
        fold_ckpt_dir = ckpt_dir + '/' + str(fold_num)
        gan_variables_file = fold_ckpt_dir + '/tf_acgan_variables_'
        if use_checkpoint:
            ckpt = tf.train.get_checkpoint_state(fold_ckpt_dir)
            if ckpt and ckpt.model_checkpoint_path:
                print ckpt.model_checkpoint_path
                model.saver.restore(sess, ckpt.model_checkpoint_path) # restore all variables
    
        start = model.get_global_step() + 1 # get last global_step and start the next one
        print "Start from:", start
        
        batch_x, batch_y, _, _ = batch_generator(train_x, train_y).next()
        batch_fake_c = np.zeros([FLAGS.BATCH_SIZE], dtype=np.int32)
        batch_z = sample_Z(FLAGS.BATCH_SIZE, FLAGS.LATENT_SIZE)
        batch_samples = model.run_samples(sess, batch_fake_c, batch_z)
        save_samples_to_file(batch_samples, batch_fake_c, fold_num, 'pre')
        
        xaxis = 0
        step = 0
        for cur_epoch in range(start, FLAGS.EPOCHS):
            disc_steps = 3
            step_ctr = 0
            for batch_x, batch_y, cur, data_len in batch_generator(train_x, train_y):
                batch_z = sample_Z(batch_x.shape[0], FLAGS.LATENT_SIZE)
                batch_fake_c = sample_C(batch_x.shape[0])
                for j in range(1):
                    _, D_loss_curr, real_acc, fake_acc, real_class_acc, fake_class_acc = model.run_D_train_step(
                        sess, batch_x, batch_y, batch_z, batch_fake_c)
                step_ctr += 1
                if step_ctr == disc_steps:
                    step_ctr = 0
                    for j in range(1):
                        batch_z = sample_Z(batch_x.shape[0], FLAGS.LATENT_SIZE)
                        g_batch_fake_c = sample_C(batch_x.shape[0])
                        _, G_loss_curr, batch_samples, batch_probs, summary = model.run_G_train_step(
                            sess, batch_x, batch_y, batch_z, g_batch_fake_c)
            
                    train_writer.add_summary(summary, step)
                    step += 1
                    generated_sequences = batch_samples
                    print('Iter: {}'.format(cur_epoch))
                    print('Instance ', cur, ' out of ', data_len)
                    print('D loss: {:.4}'. format(D_loss_curr))
                    print('G_loss: {:.4}'.format(G_loss_curr))
                    print('D real acc: ', real_acc, ' D fake acc: ', fake_acc)
                    print('D real class acc: ', real_class_acc, ' D fake class acc: ', fake_class_acc)
                    print('Samples', generated_sequences)
                    print()
                    for i in range(min(3, len(generated_sequences))):
                        for j in range(len(generated_sequences[i])):
                            if generated_sequences[i][j] == 0:
                                print '<UNK> ',
                            else:
                                word = d[generated_sequences[i][j]]
                                print word + ' ',
                        print '(' + str(g_batch_fake_c[i]) + ')\n'
                     
            if cur_epoch % 1 == 0:
                save_samples_to_file(generated_sequences, g_batch_fake_c, fold_num, cur_epoch)
            
            print 'saving model to file:'
    #         global_step.assign(cur_epoch).eval() # set and update(eval) global_step with index, cur_epoch
            model.set_global_step(cur_epoch)
    #         saver.save(sess, fold_ckpt_dir + "/model.ckpt", global_step=global_step)
            model.saver.save(sess, fold_ckpt_dir + "/model.ckpt", global_step=cur_epoch)
    #         vars = sess.run(tvars)
            vars = model.get_variables(sess)
            tvar_names = [var.name for var in tf.trainable_variables()]
            variables = dict(zip(tvar_names, vars))
            np.savez(gan_variables_file + str(cur_epoch), **variables)
            
        train_writer.close()
            
#         save_samples_to_file(generated_sequences, batch_fake_c, cur_epoch)
        
def startProgress(title):
    global progress_x
    sys.stdout.write(title + ": [" + "-"*40 + "]" + chr(8)*41)
    sys.stdout.flush()
    progress_x = 0

def progress(x):
    global progress_x
    x = int(x * 40 // 100)
    sys.stdout.write("#" * (x - progress_x))
    sys.stdout.flush()
    progress_x = x

def endProgress():
    sys.stdout.write("#" * (40 - progress_x) + "]\n")
    sys.stdout.flush()
    
def test(model, test_x, test_y, fold_num):
    print 'building graph'
    if not model.is_built:
        model.build_graph(include_optimizer=False)
    print 'testing'
    with tf.Session() as sess:
        tf.global_variables_initializer().run()
        fold_ckpt_dir = ckpt_dir + '/' + str(fold_num)
        ckpt = tf.train.get_checkpoint_state(fold_ckpt_dir)
        if not ckpt:
            raise Exception('Could not find saved model in: ' + fold_ckpt_dir)
        if ckpt and ckpt.model_checkpoint_path:
            print ckpt.model_checkpoint_path
            model.saver.restore(sess, ckpt.model_checkpoint_path) # restore all variables
        predictions = []
        startProgress('testing')
        for batch_x, batch_y, cur, data_len in batch_generator(test_x, test_y, batch_size=1):
#         for batch_x, batch_y, cur, data_len in batch_generator(test_x, test_y):
            batch_predictions = model.run_test(sess, batch_x)
            predictions.append(batch_predictions)
#             print('Instance ', cur, ' out of ', data_len)
            progress(float(cur)/float(data_len)*100)
        endProgress()
        predictions = np.concatenate(predictions)
        predictions_indices = np.argmax(predictions, axis=1)
        print_metrics(test_y, predictions_indices)
        a=1
        
def run_on_fold(args, fold_num):
    train_x, train_y, test_x, test_y = load_train_test_data(fold_num)
    model = acgan_model.ACGANModel(vague_terms, params)
#     args.train = True
    if args.train:
        train(model, train_x, train_y, fold_num)
    else:
        test(model, test_x, test_y, fold_num)
    
        
    
def main(unused_argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", help="run in train mode",
                        action="store_true")
    parser.add_argument("--xval", help="perform five-fold cross validation",
                        action="store_true")
    args = parser.parse_args()
    if args.xval:
        for fold_num in range(num_folds):
            run_on_fold(args, fold_num)
    else:
        run_on_fold(args, 0)

    localtime = time.asctime( time.localtime(time.time()) )
    print "Finished at: ", localtime       
    print('Execution time: ', (time.time() - start_time)/3600., ' hours')
    
if __name__ == '__main__':
  tf.app.run()
    
    
    
    