import os
import time
import operator

import numpy as np
import matplotlib.pyplot as plt
import cv2

from scipy import interpolate
from sklearn.utils import shuffle
from sklearn.metrics import confusion_matrix
import itertools

import pickle

import theano
import theano.tensor as T

from lasagne import random as lasagne_random
from lasagne import layers as l
from lasagne import nonlinearities
from lasagne import init
from lasagne import objectives
from lasagne import updates
from lasagne import regularization
train_path = 'dataset/train/src/'

test_path = 'dataset/val/src/'

classes = [c for c in sorted(os.listdir(train_path))]

RANDOM_SEED = 1337
RANDOM = np.random.RandomState(RANDOM_SEED)
lasagne_random.set_rng(RANDOM)

#Dataset params
DATASET_PATH = 'dataset/train/spec_44.1/'
NOISE_PATH = 'dataset/train/noise/'
MAX_SAMPLES = None                                                  
MAX_VAL_SAMPLES = None
MAX_CLASSES = None                                                   
MIN_SAMPLES_PER_CLASS = -1                                          
MAX_SAMPLES_PER_CLASS = 1500                                       
SORT_CLASSES_ALPHABETICALLY = True                               
VAL_SPLIT = 0.05                                                    
USE_CACHE = False                                                  

#Ensamble params
SAMPLE_RANGE = [None, None]
CLASS_RANGE = [None, None]
CLASS_NAMES = []
MULTI_LABEL = False
VAL_HAS_MULTI_LABEL = False
MEAN_TARGETS_PER_IMAGE = 2
IM_SIZE = (512, 256) #(width, height)
IM_DIM = 1
IM_AUGMENTATION = {#'type':[probability, value]
                   'roll':[0.5, (0.0, 0.05)], 
                   'noise':[0.1, 0.01],
                   'noise_samples':[0.1, 1.0],
                   #'brightness':[0.5, (0.25, 1.25)],
                   #'crop':[0.5, 0.07],
                   #'flip': [0.25, 1]
                   }

SAME_CLASS_AUGMENTATION = True
MAX_SAME_CLASS_COMBINATIONS = 5

#General model params
MODEL_TYPE = 1 #1, 2 or 3 - see working notes for details
DROPOUT = 0.5
NONLINEARITY = nonlinearities.elu #nonlinearities.rectify
INIT_GAIN = 1.0 #1.0 if elu, sqrt(2) if rectify

#Training params
BATCH_SIZE = 128
LEARNING_RATE = {1:0.01, 35:0.0001, 55:0.00001} #epoch:lr
LR_DESCENT = True
L2_WEIGHT = 1e-4
OPTIMIZER='adam' #'adam' or 'nesterov'
EPOCHS = 55
RANDOMIZE_TRAIN_SET = True

#Confusion matrix params
CONFMATRIX_MAX_CLASSES = 100
NORMALIZE_CONFMATRIX = True

#Model import/export params
MODEL_PATH = 'model/'
PRETRAINED_MODEL = None #'pretrained_model.pkl'
LOAD_OUTPUT_LAYER = True
EPOCH_START = 1
RUN_NAME = 'TUCMI_Example_Run'
SIMPLE_LOG_MODE = True
SNAPSHOT_EPOCHS = [10, 20, 30, 40, 50] #[-1] saves after every epoch
SAVE_AFTER_INTERRUPT = True


def parseDataset():

    #we use subfolders as class labels
    classes = [folder for folder in sorted(os.listdir(DATASET_PATH)) if folder in CLASS_NAMES or len(CLASS_NAMES) == 0]
    if not SORT_CLASSES_ALPHABETICALLY:
        classes = shuffle(classes, random_state=RANDOM)
    classes = classes[:MAX_CLASSES]

    #limit number of classes for ensemble training?
    classes = classes[CLASS_RANGE[0]:CLASS_RANGE[1]]

    #now we enlist all image paths for each class
    images = []
    tclasses = []
    sample_count = {}
    for c in classes:
        c_images = [os.path.join(DATASET_PATH, c, path) for path in os.listdir(os.path.join(DATASET_PATH, c))][:MAX_SAMPLES_PER_CLASS]

        #add images to dataset if number of samples in specific range (important only for ensemble training)
        if not SAMPLE_RANGE[1] or len(c_images) in range(SAMPLE_RANGE[0], SAMPLE_RANGE[1]):
            sample_count[c] = len(c_images)
            images += c_images
            tclasses.append(c)

            #Do we want to correct class imbalance?
            #This will affect validation scores as we use some samples in TRAIN and VAL
            while sample_count[c] < MIN_SAMPLES_PER_CLASS:
                images += [c_images[RANDOM.randint(0, len(c_images))]]
                sample_count[c] += 1

    classes = tclasses    

    #shuffle image paths
    images = shuffle(images, random_state=RANDOM)[:MAX_SAMPLES]

    #validation split
    vsplit = int(len(images) * VAL_SPLIT)
    train = images[:-vsplit]
    val = images[-vsplit:][:MAX_VAL_SAMPLES]

    #load noise samples
    noise = shuffle([os.path.join(NOISE_PATH, path) for path in os.listdir(NOISE_PATH)], random_state=RANDOM)

    #show classes if needed for testing
    #print classes

    #show some stats
    print "CLASSES:", len(classes)
    print "CLASS LABELS:", sorted(sample_count.items(), key=operator.itemgetter(1))
    print "TRAINING IMAGES:", len(train)
    print "VALIDATION IMAGES:", len(val)
    print "NOISE SAMPLES:", len(noise)

    return classes, train, val, noise
CLASSES, TRAIN, VAL, NOISE = parseDataset()
NUM_CLASSES = len(CLASSES)
CACHE = {}
def openImage(path, useCache=USE_CACHE):

    global CACHE

    #using a dict {path:image} cache saves some time after first epoch
    #but may consume a lot of RAM
    if path in CACHE:
        return CACHE[path]
    else:

        #open image
        img = cv2.imread(path)

        #DEBUG
        try:
            h, w = img.shape[:2]
        except:
            print "IMAGE NONE-TYPE:", path

        #original image dimensions
        try:
            h, w, d = img.shape

            #to gray?
            if IM_DIM == 1:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
        except:
            h, w = img.shape

            #to color?
            if IM_DIM == 3:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        #resize to conv input size
        img = cv2.resize(img, (IM_SIZE[0], IM_SIZE[1]))

        #convert to floats between 0 and 1
        img = np.asarray(img / 255., dtype='float32')  
        
        if useCache:
            CACHE[path] = img
        return img

def imageAugmentation(img):

    AUG = IM_AUGMENTATION

    #Random Crop (without padding)
    if 'crop' in AUG and RANDOM.choice([True, False], p=[AUG['crop'][0], 1 - AUG['crop'][0]]):
        h, w = img.shape[:2]
        cropw = RANDOM.randint(1, int(float(w) * AUG['crop'][1]))
        croph = RANDOM.randint(1, int(float(h) * AUG['crop'][1]))
        img = img[croph:-croph, cropw:-cropw]
        img = cv2.resize(img, (IM_SIZE[0], IM_SIZE[1]))

    #Flip - 1 = Horizontal, 0 = Vertical
    if 'flip' in AUG and RANDOM.choice([True, False], p=[AUG['flip'][0], 1 - AUG['flip'][0]]):    
        img = cv2.flip(img, AUG['flip'][1])

    #Wrap shift (roll up/down and left/right)
    if 'roll' in AUG and RANDOM.choice([True, False], p=[AUG['roll'][0], 1 - AUG['roll'][0]]):
        img = np.roll(img, int(img.shape[0] * (RANDOM.uniform(-AUG['roll'][1][1], AUG['roll'][1][1]))), axis=0)
        img = np.roll(img, int(img.shape[1] * (RANDOM.uniform(-AUG['roll'][1][0], AUG['roll'][1][0]))), axis=1)

    #substract/add mean
    if 'mean' in AUG and RANDOM.choice([True, False], p=[AUG['mean'][0], 1 - AUG['mean'][0]]):   
        img += np.mean(img) * AUG['mean'][1]

    #gaussian noise
    if 'noise' in AUG and RANDOM.choice([True, False], p=[AUG['noise'][0], 1 - AUG['noise'][0]]):
        img += RANDOM.normal(0.0, RANDOM.uniform(0, AUG['noise'][1]**0.5), img.shape)
        img = np.clip(img, 0.0, 1.0)

    #add noise samples
    if 'noise_samples' in AUG and RANDOM.choice([True, False], p=[AUG['noise_samples'][0], 1 - AUG['noise_samples'][0]]):
        img += openImage(NOISE[RANDOM.choice(range(0, len(NOISE)))], True) * AUG['noise_samples'][1]
        img -= img.min(axis=None)
        img /= img.max(axis=None)

    #adjust brightness
    if 'brightness' in AUG and RANDOM.choice([True, False], p=[AUG['brightness'][0], 1 - AUG['brightness'][0]]):
        img *= RANDOM.uniform(AUG['brightness'][1][0], AUG['brightness'][1][1])
        img = np.clip(img, 0.0, 1.0)
        
    #show
    #cv2.imshow("AUG", img)#.reshape(IM_SIZE[1], IM_SIZE[0], IM_DIM))
    #cv2.waitKey(-1)

    return img
    
def loadImageAndTarget(path, doAugmentation=True):

    #here we open the image
    img = openImage(path)

    #image augmentation?
    if IM_AUGMENTATION != None and doAugmentation:
        img = imageAugmentation(img)
    
    #we want to use subfolders as class labels
    label = path.split("/")[-2]

    #we need to get the index of our label from CLASSES
    index = CLASSES.index(label)

    #allocate array for target
    target = np.zeros((NUM_CLASSES), dtype='float32')

    #we set our target array = 1.0 at our label index, all other entries remain 0.0
    target[index] = 1.0
    
    #transpose image if dim=3
    try:
        img = np.transpose(img, (2, 0, 1))
    except:
        pass

    #we need a 4D-vector for our image and a 2D-vector for our targets
    img = img.reshape(-1, IM_DIM, IM_SIZE[1], IM_SIZE[0])
    target = target.reshape(-1, NUM_CLASSES)

    return img, target

def getSameClassAugmentation(x, y):

    #are there some samples with the same class label?
    scl = np.where(np.sum(y, axis=0) > 1)[0]
    acnt = 0
    while scl.shape[0] > 0 and acnt < MAX_SAME_CLASS_COMBINATIONS:

        #randomly chosen class
        c = RANDOM.choice(scl)

        #get all samples of this selected class
        s = []
        for i in range(0, y.shape[0]):
            if y[i][c] == 1:
              s.append(i)

        #combine first two samples
        x[s[0]] += x[s[1]]

        #re-normalize new image
        x[s[0]] -= x[s[0]].min(axis=None)
        x[s[0]] /= x[s[0]].max(axis=None)

        #remove augmented class
        scl = np.delete(scl, np.where(scl == c))
        acnt += 1

        #show
        #print scl, acnt, c
        #cv2.imshow("BA", x[s[0]].reshape(IM_SIZE[1], IM_SIZE[0], IM_DIM))
        #cv2.waitKey(-1)

    return x, y        

def getAugmentedBatches(x, y):

    #augment batch until desired number of target labels per image is reached
    while np.mean(np.sum(y, axis=1)) < MEAN_TARGETS_PER_IMAGE:

        #get two images to combine (we try to prevent i == j (which could result in infinite loops) with excluding ranges)
        i = RANDOM.choice(range(1, x.shape[0] - 1))
        j = RANDOM.choice(range(0, i) + range(i + 1, x.shape[0]))

        #add images
        x[i] += x[j]

        #re-normalize new image
        x[i] -= x[i].min(axis=None)
        x[i] /= x[i].max(axis=None)

        #combine targets (makes this task a multi-label classification!)
        y[i] = np.logical_or(y[i], y[j])

        #TODO: We still might end up in an infinite loop
        #and should add a break in case something is fishy

        #show
        #cv2.imshow("BA", x[i].reshape(IM_SIZE[1], IM_SIZE[0], IM_DIM))
        #cv2.waitKey(-1)
    
    return x, y

#Loading images with CPU background threads during GPU forward passes saves a lot of time
#Credit: J. Schlüter (https://github.com/Lasagne/Lasagne/issues/12)
def threadedBatchGenerator(generator, num_cached=10):
    
    import Queue
    queue = Queue.Queue(maxsize=num_cached)
    sentinel = object()  # guaranteed unique reference

    #define producer (putting items into queue)
    def producer():
        for item in generator:
            queue.put(item)
        queue.put(sentinel)

    #start producer (in a background thread)
    import threading
    thread = threading.Thread(target=producer)
    thread.daemon = True
    thread.start()

    #run as consumer (read items from queue, in current thread)
    item = queue.get()
    while item is not sentinel:
        yield item
        queue.task_done()
        item = queue.get()

def getDatasetChunk(split):

    #get batch-sized chunks of image paths
    for i in xrange(0, len(split), BATCH_SIZE):
        yield split[i:i+BATCH_SIZE]

def getNextImageBatch(split=TRAIN, doAugmentation=True, batchAugmentation=MULTI_LABEL): 

    #fill batch
    for chunk in getDatasetChunk(split):

        #allocate numpy arrays for image data and targets
        x_b = np.zeros((BATCH_SIZE, IM_DIM, IM_SIZE[1], IM_SIZE[0]), dtype='float32')
        y_b = np.zeros((BATCH_SIZE, NUM_CLASSES), dtype='float32')
        
        ib = 0
        for path in chunk:

            try:
            
                #load image data and class label from path
                x, y = loadImageAndTarget(path, doAugmentation)

                #pack into batch array
                x_b[ib] = x
                y_b[ib] = y
                ib += 1

            except:
                continue

        #trim to actual size
        x_b = x_b[:ib]
        y_b = y_b[:ib]

        #same class augmentation?
        if doAugmentation and SAME_CLASS_AUGMENTATION and x_b.shape[0] > 2:
            x_b, y_b = getSameClassAugmentation(x_b, y_b)

        #batch augmentation?
        if batchAugmentation and x_b.shape[0] >= BATCH_SIZE // 2:
            x_b, y_b = getAugmentedBatches(x_b, y_b)

        #instead of return, we use yield
        yield x_b, y_b

################## BUILDING THE MODEL ###################
def buildModel(mtype=1):

    print "BUILDING MODEL TYPE", mtype, "..."

    #default settings (Model 1)
    filters = 64
    first_stride = 2
    last_filter_multiplier = 16

    #specific model type settings (see working notes for details)
    if mtype == 2:
        first_stride = 1
    elif mtype == 3:
        filters = 32
        last_filter_multiplier = 8

    #input layer
    net = l.InputLayer((None, IM_DIM, IM_SIZE[1], IM_SIZE[0]))

    #conv layers
    net = l.batch_norm(l.Conv2DLayer(net, num_filters=filters, filter_size=7, pad='same', stride=first_stride, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
    net = l.MaxPool2DLayer(net, pool_size=2)

    if mtype == 2:
        net = l.batch_norm(l.Conv2DLayer(net, num_filters=filters, filter_size=5, pad='same', stride=1, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
        net = l.MaxPool2DLayer(net, pool_size=2)

    net = l.batch_norm(l.Conv2DLayer(net, num_filters=filters * 2, filter_size=5, pad='same', stride=1, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
    net = l.MaxPool2DLayer(net, pool_size=2)

    net = l.batch_norm(l.Conv2DLayer(net, num_filters=filters * 4, filter_size=3, pad='same', stride=1, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
    net = l.MaxPool2DLayer(net, pool_size=2)

    net = l.batch_norm(l.Conv2DLayer(net, num_filters=filters * 8, filter_size=3, pad='same', stride=1, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
    net = l.MaxPool2DLayer(net, pool_size=2)

    net = l.batch_norm(l.Conv2DLayer(net, num_filters=filters * last_filter_multiplier, filter_size=3, pad='same', stride=1, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
    net = l.MaxPool2DLayer(net, pool_size=2)

    print "\tFINAL POOL OUT SHAPE:", l.get_output_shape(net) 

    #dense layers
    net = l.batch_norm(l.DenseLayer(net, 512, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
    net = l.DropoutLayer(net, DROPOUT)  
    net = l.batch_norm(l.DenseLayer(net, 512, W=init.HeNormal(gain=INIT_GAIN), nonlinearity=NONLINEARITY))
    net = l.DropoutLayer(net, DROPOUT)  

    #Classification Layer
    if MULTI_LABEL:
        net = l.DenseLayer(net, NUM_CLASSES, nonlinearity=nonlinearities.sigmoid, W=init.HeNormal(gain=1))
    else:
        net = l.DenseLayer(net, NUM_CLASSES, nonlinearity=nonlinearities.softmax, W=init.HeNormal(gain=1))

    print "...DONE!"

    #model stats
    print "MODEL HAS", (sum(hasattr(layer, 'W') for layer in l.get_all_layers(net))), "WEIGHTED LAYERS"
    print "MODEL HAS", l.count_params(net), "PARAMS"

    return net

NET = buildModel(MODEL_TYPE)
##################  MODEL SAVE/LOAD  ####################
BEST_PARAMS = None
BEST_EPOCH = 0
def saveParams(epoch, params=None):    
    print "EXPORTING MODEL PARAMS...",
    if params == None:
        params = l.get_all_param_values(NET)
    net_filename = MODEL_PATH + "birdCLEF_" + RUN_NAME + "_model_params_epoch_" + str(epoch) + ".pkl"
    if not os.path.exists(MODEL_PATH):
        os.makedirs(MODEL_PATH)
    with open(net_filename, 'w') as f:
        pickle.dump(params, f) 
    print "DONE!"

def loadParams(epoch, filename=None):
    print "IMPORTING MODEL PARAMS...",
    if filename == None:
        net_filename = MODEL_PATH + "birdCLEF_" + RUN_NAME + "_model_params_epoch_" + str(epoch) + ".pkl"
    else:
        net_filename = MODEL_PATH + filename
    with open(net_filename, 'rb') as f:
        params = pickle.load(f)
    if LOAD_OUTPUT_LAYER:
        l.set_all_param_values(NET, params)
    else:
        l.set_all_param_values(l.get_all_layers(NET)[:-1], params[:-2])
    print "DONE!"

if PRETRAINED_MODEL != None:
    loadParams(-1, PRETRAINED_MODEL)
#################### LOSS FUNCTION ######################
def calc_loss(prediction, targets):

    #categorical crossentropy is the best choice for a multi-class softmax output
    loss = T.mean(objectives.categorical_crossentropy(prediction, targets))
    
    return loss

def calc_loss_multi(prediction, targets):
    
    #we need to clip predictions when calculating the log-loss
    prediction = T.clip(prediction, 0.0000001, 0.9999999)

    #binary crossentropy is the best choice for a multi-class sigmoid output
    loss = T.mean(objectives.binary_crossentropy(prediction, targets))
    
    return loss

#theano variable for the class targets
targets = T.matrix('targets', dtype=theano.config.floatX)

#get the network output
prediction = l.get_output(NET)

#we use L2 Norm for regularization
l2_reg = regularization.regularize_layer_params(NET, regularization.l2) * L2_WEIGHT

#calculate the loss
if MULTI_LABEL:
    loss = calc_loss_multi(prediction, targets) + l2_reg
else:
    loss = calc_loss(prediction, targets) + l2_reg

################# ACCURACY FUNCTION #####################
def calc_accuracy(prediction, targets):

    #we can use the lasagne objective categorical_accuracy to determine the top1 single label accuracy
    a = T.mean(objectives.categorical_accuracy(prediction, targets, top_k=1))
    
    return a

def calc_accuracy_multi(prediction, targets):

    #we can use the lasagne objective binary_accuracy to determine the multi label accuracy
    a = T.mean(objectives.binary_accuracy(prediction, targets))
    
    return a

#calculate accuracy
if MULTI_LABEL and VAL_HAS_MULTI_LABEL:
    accuracy = calc_accuracy_multi(prediction, targets)
else:
    accuracy = calc_accuracy(prediction, targets)

####################### UPDATES #########################
#we use dynamic learning rates which change after some epochs
lr_dynamic = T.scalar(name='learning_rate')
                    
#get all trainable parameters (weights) of our net
params = l.get_all_params(NET, trainable=True)

#we use the adam update
if OPTIMIZER == 'adam':
    param_updates = updates.adam(loss, params, learning_rate=lr_dynamic, beta1=0.5)
elif OPTIMIZER == 'nesterov':
    param_updates = updates.nesterov_momentum(loss, params, learning_rate=lr_dynamic, momentum=0.9)

#################### TRAIN FUNCTION ######################
#the theano train functions takes images and class targets as input
print "COMPILING THEANO TRAIN FUNCTION...",
start = time.time()
train_net = theano.function([l.get_all_layers(NET)[0].input_var, targets, lr_dynamic], loss, updates=param_updates)
print "DONE! (", int(time.time() - start), "s )"

################# PREDICTION FUNCTION ####################
#we need the prediction function to calculate the validation accuracy
#this way we can test the net during/after training
net_output = l.get_output(NET, deterministic=True)

print "COMPILING THEANO TEST FUNCTION...",
start = time.time()
test_net = theano.function([l.get_all_layers(NET)[0].input_var, targets], [net_output, loss, accuracy])
print "DONE! (", int(time.time() - start), "s )"

################## CONFUSION MATRIX #####################
cmatrix = []
def clearConfusionMatrix():

    global cmatrix

    #allocate empty matrix
    cmatrix = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype='int32')

def updateConfusionMatrix(p, t):

    global cmatrix

    #get class indices for prediction and target
    targets = np.argmax(t, axis=1)
    predictions = np.argmax(p, axis=1)

    #add up confusion matrices of validation batches
    cmatrix += confusion_matrix(targets, predictions, labels=range(0, NUM_CLASSES))

def showConfusionMatrix(epoch):

    #new figure
    plt.figure(0, figsize=(35, 35), dpi=72)
    plt.clf()

    #get additional metrics
    pr, re, f1 = calculateMetrics()

    #normalize?
    if NORMALIZE_CONFMATRIX:
        global cmatrix
        cmatrix = np.around(cmatrix.astype('float') / cmatrix.sum(axis=1)[:, np.newaxis] * 100.0, decimals=1)

    #show matrix
    plt.imshow(cmatrix[:CONFMATRIX_MAX_CLASSES, :CONFMATRIX_MAX_CLASSES], interpolation='nearest', cmap=plt.cm.Blues)
    plt.title('Confusion Matrix\n' +
              RUN_NAME + ' - Epoch ' + str(epoch) +
              '\nTrain Samples: ' + str(len(TRAIN)) + ' Validation Samples: ' + str(len(VAL)) +              
              '\nmP: ' + str(np.mean(pr)) + ' mF1: ' + str( np.mean(f1)), fontsize=22)

    #tick marks
    tick_marks = np.arange(min(CONFMATRIX_MAX_CLASSES, NUM_CLASSES))
    plt.xticks(tick_marks, CLASSES[:CONFMATRIX_MAX_CLASSES], rotation=90)
    plt.yticks(tick_marks, CLASSES[:CONFMATRIX_MAX_CLASSES])

    #labels
    thresh = cmatrix.max() / 2.
    for i, j in itertools.product(range(min(CONFMATRIX_MAX_CLASSES, cmatrix.shape[0])), range(min(CONFMATRIX_MAX_CLASSES, cmatrix.shape[1]))):
        plt.text(j, i, cmatrix[i, j], 
                 horizontalalignment="center", verticalalignment="center",
                 color="white" if cmatrix[i, j] > thresh else "black", fontsize=8)

    #axes labels
    plt.tight_layout()
    plt.ylabel('Target label', fontsize=16)
    plt.xlabel('Predicted label', fontsize=16)

    #fontsize
    plt.rc('font', size=12)

    #save plot
    global cmcnt
    if not os.path.exists('confmatrix'):
        os.makedirs('confmatrix')
    plt.savefig('confmatrix/' + RUN_NAME + '_' + str(epoch) + '.png')

def calculateMetrics():

    #allocate arrays
    pr = []
    re = []
    f1 = []

    #parse rows and columns of confusion matrix
    for i in range(0, cmatrix.shape[0]):

        #true positives, false positves, false negatives
        tp = float(cmatrix[i][i])
        fp = float(np.sum(cmatrix, axis=1)[i] - tp)
        fn = float(np.sum(cmatrix, axis=0)[i] - tp)

        #precision
        if tp > 0 or fp > 0:
            p = tp / (tp + fp)
        else:
            p = 0
        pr.append(p)

        #recall
        if tp > 0 or fn > 0:
            r = tp / (tp + fn)
        else:
            r = 0
        re.append(r)

        #f1 measure
        if p > 0 or r > 0:
            f = 2 * ((p * r) / (p + r))
        else:
            f = 0
        f1.append(f)
    
    return pr, re, f1

###################### PROGRESS #########################
batches_per_epoch = len(TRAIN + VAL) // BATCH_SIZE + 1
avg_duration = []
last_update = -1
def showProgress(stat, duration, current, end=batches_per_epoch, update_interval=5, simple_mode=False):

    #epochs might take a lot of time, so we want some kind of progress bar
    #this approach is not very sophisticated, but it does the job :)
    #you should use simple_mode=True if run with IDLE and simple_mode=False if run on command line

    global avg_duration
    global last_update

    #time left
    avg_duration.append(duration)
    avg_duration = avg_duration[-10:]
    r = int(abs(end - current) * np.mean(avg_duration) / 60) + 1

    #percentage
    p = int(current / float(end) * 100)
    progress = ""
    for s in xrange(update_interval, 100, update_interval):
        if s <= p:
            progress += "="
        else:
            progress += " "        

    #status line
    if p > last_update and p % update_interval == 0 or last_update == -1:
        if simple_mode:
            if current == 1:
                print stat.upper() + ": [",
            else:
                print "=",
            if current == end:
                print "]",
        else:
            print stat.upper() + ": [" + progress + "] BATCHES " + str(current) + "/" + str(end) + " (" + str(p) + "%) - " + str(r) + " min REMAINING\r",
        last_update = p

###################### TRAINING #########################
print "START TRAINING..."
train_loss = []
val_loss = []
val_accuracy = []
max_acc = -1
lr = LEARNING_RATE[LEARNING_RATE.keys()[0]]
SAVE_MODEL_AFTER_TRAINING = True

#train for some epochs...
for epoch in range(EPOCH_START, EPOCHS + 1):

    try:

        #start timer
        start = time.time()

        #reset confusion matrix
        clearConfusionMatrix()

        #adjust learning rate (interpolate or steps)
        if LR_DESCENT:
            lr_keys = np.array(LEARNING_RATE.keys() + [EPOCHS], dtype='float32')
            lr_values = np.array(LEARNING_RATE.values() + [LEARNING_RATE.values()[-1]], dtype='float32')
            lr_func = interpolate.interp1d(lr_keys, lr_values, kind='linear')
            lr = np.float32(lr_func(max(LEARNING_RATE.keys()[0], epoch - 1)))
        else:
            if epoch in LEARNING_RATE:
                lr = LEARNING_RATE[epoch]

        #shuffle dataset (this way we get "new" batches every epoch)
        if RANDOMIZE_TRAIN_SET:
            TRAIN = shuffle(TRAIN, random_state=RANDOM)

        #time
        bstart = time.time()
        last_update = -1

        #iterate over train split batches and calculate mean loss for epoch
        t_l = []
        bcnt = 0
        for image_batch, target_batch in threadedBatchGenerator(getNextImageBatch()):            

            #calling the training functions returns the current loss
            loss = train_net(image_batch, target_batch, lr)
            t_l.append(loss)

            #exploding gradient and loss is NaN?
            if t_l != t_l:
                print "\nERROR: LOSS IS NaN!"
                break
            
            bcnt += 1

            #show progress
            showProgress("EPOCH " + str(epoch), (time.time() - bstart), bcnt, simple_mode=SIMPLE_LOG_MODE)
            bstart = time.time()

        #we validate our net every epoch and pass our validation split through as well
        v_l = []
        v_a = []
        for image_batch, target_batch in threadedBatchGenerator(getNextImageBatch(VAL, False, VAL_HAS_MULTI_LABEL)):

            #calling the test function returns the net output, loss and accuracy
            prediction_batch, loss, acc = test_net(image_batch, target_batch)
            v_l.append(loss)
            v_a.append(acc)

            #save predicions and targets for confusion matrix
            updateConfusionMatrix(prediction_batch, target_batch)

            bcnt += 1   

            #show progress
            showProgress("EPOCH " + str(epoch), (time.time() - bstart), bcnt, simple_mode=SIMPLE_LOG_MODE)
            bstart = time.time()

        #stop timer
        end = time.time()

        #calculate stats for epoch
        train_loss.append(np.mean(t_l))
        val_loss.append(np.mean(v_l))
        val_accuracy.append(np.mean(v_a))

        #print stats for epoch
        print "TRAIN LOSS:", train_loss[-1],
        print "VAL LOSS:", val_loss[-1],
        print "VAL ACCURACY:", (int(val_accuracy[-1] * 1000) / 10.0), "%",
        print "LR:", lr,
        print "TIME:", (int((end - start) * 10) / 10.0), "s"

        #log max accuracy and save best params
        acc = (int(val_accuracy[-1] * 1000) / 10.0)
        if  acc > max_acc:
            max_acc = acc
            BEST_PARAMS = l.get_all_param_values(NET)
            BEST_EPOCH = epoch

        #show confusion matrix
        showConfusionMatrix(epoch)

        #save snapshot?
        if epoch in SNAPSHOT_EPOCHS or SNAPSHOT_EPOCHS[0] == -1:
            saveParams(epoch)

    except KeyboardInterrupt:
        SAVE_MODEL_AFTER_TRAINING = SAVE_AFTER_INTERRUPT
        break

print "TRAINING DONE!"
print "MAX ACC: ", max_acc

#save best model params
if SAVE_MODEL_AFTER_TRAINING:
    saveParams(BEST_EPOCH, BEST_PARAMS)



import os
from shutil import copyfile
from sklearn.utils import shuffle
 species and class id
#Use birdCLEF_sort_data.py in order to sort wav files accordingly
train_path = 'dataset/train/src/'

#Specify target folder for validation split
test_path = 'dataset/val/src/'

######################################################

#get classes from subfolders
classes = [c for c in sorted(os.listdir(train_path))]

#get files for classes
for c in classes:

    #shuffle files
    files = shuffle([train_path + c + "/" + f for f in os.listdir(train_path + c)], random_state=1337)

    #choose amount of files for validation split from each class
    #we want at least 1 sample per class (2 if sample count os between 12 and 20)
    #we take 10% of the samples if sample count > 20
    if len(files) <= 12:
        num_test_files = 1
    elif len(files) > 12 and len(files) <= 20:
        num_test_files = 2
    else:
        num_test_files = int(len(files) * 0.1)

    test_files = files[:num_test_files]
    print c, len(files), len(test_files)

    #copy test files for validation to target folder
    for tf in test_files:

        #copy test file
        new_path = tf.replace(train_path, test_path).rsplit("/", 1)[0]
        if not os.path.exists(new_path):
            os.makedirs(new_path)
        copyfile(tf, tf.replace(train_path, test_path))

        #remove test file from train
        #Note: You might want to test the script first before deleting any files :)
        os.remove(tf)
        
    #break
