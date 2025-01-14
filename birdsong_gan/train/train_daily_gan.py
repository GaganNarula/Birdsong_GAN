import os
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
import torch.optim as optim
from torch.optim.lr_scheduler import ExponentialLR
from torch.utils.data import DataLoader

import os
from os.path import join
import joblib
import argparse
import shutil
import random
import numpy as np
import librosa as lc
from datetime import datetime
import itertools
import gc
import pdb
import json


from birdsong_gan.configs.cfg import *
from birdsong_gan.data.dataset import bird_dataset_single_hdf, transform, inverse_transform
from birdsong_gan.models.nets_16col_residual import _netD, _netE, _netG, InceptionNet, weights_init

from birdsong_gan.reconstruction_error.pca import learn_pca_model

from birdsong_gan.hmm.hmm import learnKmodels_getbest
from birdsong_gan.hmm.hmm_utils import munge_sequences, full_entropy, create_output, number_of_active_states_viterbi, hmm_num_free_params
from birdsong_gan.utils.utils import overlap_encode, overlap_decode, gagan_save_spect, save_audio_sample, \
    rescale_spectrogram, make_output_folder



gan_opts = {'datapath': '', 'outf': '', 'birdname':'',
            'distance_fun': 'L1', 'subset_age_weights' : [0., 1.], 
            'workers': 6, 'batchSize': 128, 'chain_networks': False,
            'imageH': 129, 'imageW': 16, 'noverlap':0, 'nz': 16,'nc': 1, 'ngf': 128,
            'ndf': 128,'nepochs': 10, 'lr_schedule': False,
            'lr': 1e-4, 'lambdaa': 100., 'zreg_weight': 1, 'schedule_lr':False, 'd_noise': 0.1,
            'beta1': 0.5, 'cuda': True, 'ngpu': 1, 'nreslayers': 3, 'z_reg':False, 'mds_loss':False,
            'netG': '','netE': '','netD1':'','netD2':'','netD3':'', 'log_every': 300,
            'sample_rate': 16000.,'noise_dist': 'normal','z_var': 1.,'nfft': 256, 'get_audio': False,
            'min_num_batches': 50, 'make_run_folder': False, 'model_checkpoint_every': 3,
            'manualSeed': [], 'do_pca': True, 'npca_samples': 1e6, 'npca_components': 0.98,
           'get_FID_score': False, 'train_all_days': False, 'skip_network_training': False}

hmm_opts = {'hidden_state_size' : [5, 10, 15, 20, 30, 50, 75, 100], 'covariance_type' : 'spherical', 
           'fit_params' : 'stmc', 'transmat_prior' : 1., 'n_iter' : 300, 'tolerance' : 0.01,
            'covars_prior' : 1., 'init_params' : 'kmeans',
            'hmm_train_proportion' : 0.8, 'nsamplesteps' : 64, 'nsamps': 10,
            'sample_var': 0., 'sample_invtemperature' : 1.,
            'munge' : False, 'munge_len' : 50,
            'n_restarts': 1, 'do_chaining': False,
            'min_seq_multiplier': 5, 'cuda' : True, 'hmm_random_state' : 0,
            'last_day': -1, 'normalize_logl':True,
            'start_from_day' : 0, 'end_at': -1,
            'get_audio': False
           }

# opts structure to use
opts = {**gan_opts, **hmm_opts}



# some cuda / cudnn settings for memory issues#
torch.backends.cuda.matmul.allow_tf32 = True
cudnn.deterministic = True
cudnn.benchmark = True
#cudnn.allow_tf32 = True


# useful to flip labels randomly
def true_wp(prob, size, device):
    # generate a uniform random number
    p = torch.rand(size).to(device).float()
    # if prob = 0.9, most of the time, this will be True
    p = (p < prob).float()
    return p 


class Model:
    """Model container for the GAN.

        The container contains the model, a GAN training method, a 
        HMM training method and sampling + checkpointing methods.
    """

    def __init__(self, dataset, outpath, opts, init_nets=None):
        
        self.opts = opts

        # make a folder to save results
        self.outpath = outpath

        self.dataset = dataset

        self.nz = opts['nz']
        self.ngf = opts['ngf']
        self.ndf = opts['ndf']

        if opts['cuda']:
            self.device = 'cuda:0'
        else:
            self.device = 'cpu'

        if init_nets is None:
            self._init_networks(opts['netG'], opts['netE'], opts['netD1'], opts['netD2'],
                                 opts['netD3'])
        else:
            self._set_networks(*init_nets)

        # Define loss function
        self.criterion_gan = nn.BCELoss()
        if opts['distance_fun']=='L1':
            print('Using L1 loss')
            self.criterion_dist = nn.L1Loss()
        else:
            print('Using L2 loss')
            self.criterion_dist = nn.MSELoss()


        # downsample function for reconstruction error 
        self.downsample_pth = torch.nn.AvgPool2d(3, stride=4)

        
        # probability that a label is wrongly labelled
        self.d_prob = torch.FloatTensor([1. - opts['d_noise']]).to(self.device)

        self.X = None # data arrays (lists) for hmm learning
        self.Z = None # encoded latent vectors for hmm learning

    def _set_networks(self, netG, netE, netD1, netD2, netD3):
        self.netG = netG
        self.netE = netE
        self.netD1 = netD1
        self.D2 = netD2
        self.D3 = netD3

    def _init_networks(self, netGpath='', netEpath='', netD1path='', netD2path='', netD3path=''):

        # custom weights initialization called on networks
        self.netG = _netG(self.nz, self.ngf, 1)
        self.netG.apply(weights_init)
        if netGpath != '':
            self.netG.load_state_dict(torch.load(netGpath))

        self.netD1 = _netD(self.ndf, 1)
        self.netD1.apply(weights_init)
        if netD1path != '':
            self.netD1.load_state_dict(torch.load(netD1path))

        self.netD2 = _netD(self.ndf, 1)
        self.netD2.apply(weights_init)
        if netD2path != '':
            self.netD2.load_state_dict(torch.load(netD2path))

        # inception net
        self.netD3 = InceptionNet(self.ndf, 1)
        self.netD3.apply(weights_init)
        if netD3path != '':
            self.netD3.load_state_dict(torch.load(netD3path))

        self.netE = _netE(self.nz, self.ndf, 1)
        self.netE.apply(weights_init)
        if netEpath != '':
            self.netE.load_state_dict(torch.load(netEpath))

        if self.opts['cuda']:
            self.netD1 = self.netD1.to(self.device)
            self.netD2 = self.netD2.to(self.device)
            self.netD3 = self.netD3.to(self.device)
            self.netG = self.netG.to(self.device)
            self.netE = self.netE.to(self.device)

        # setup optimizers
        self.optimizerD1 = optim.Adam(self.netD1.parameters(), lr=self.opts['lr'], 
                                betas = (self.opts['beta1'], 0.999))
        self.optimizerD2 = optim.Adam(self.netD2.parameters(), lr=self.opts['lr'], 
                                betas = (self.opts['beta1'], 0.999))
        self.optimizerD3 = optim.Adam(self.netD3.parameters(), lr=self.opts['lr'], 
                                betas = (self.opts['beta1'], 0.999))
        self.optimizerG = optim.Adam(itertools.chain(self.netG.parameters(),
                                            self.netE.parameters()), lr=self.opts['lr'],
                                            betas = (self.opts['beta1'], 0.999))
        
        if self.opts['lr_schedule']:
            gamma=0.9 # multiply lr by this every time scheduler is called
            self.schedulerG = ExponentialLR(self.optimizerG,gamma,verbose=True)
            self.schedulerD1 = ExponentialLR(self.optimizerD1,gamma,verbose=True)
            self.schedulerD2 = ExponentialLR(self.optimizerD2,gamma,verbose=True)
            self.schedulerD3 = ExponentialLR(self.optimizerD3,gamma,verbose=True)

    def _scheduler_step(self):
        self.schedulerG.step()
        self.schedulerD1.step()
        self.schedulerD2.step()
        self.schedulerD3.step()
        
    def make_daily_dataloader(self, day=0):
        # make tensor dataset
        TD, _ = self.dataset.make_chunk_tensor_dataset(day, imageW=self.opts['imageW'],
                                                            shuffle_chunks=True)
        self.X = self.dataset.get(day, nsamps=-1)
        return DataLoader(TD, batch_size= self.opts['batchSize'], sampler = None,
                                    shuffle=True, num_workers=int(self.opts['workers']),
                                    drop_last = True)
    
    def make_single_dataloader(self):
        return DataLoader(self.dataset, batch_size= self.opts['batchSize'], sampler = None,
                                    shuffle=True, num_workers=int(self.opts['workers']),
                                    drop_last = True)
    
    def train_network(self, traindataloader, day=None):
        
        #losses
        minibatchLossD1 = []
        minibatchLossG1_rec = []
        minibatchLossG1_gan = []
        minibatchLossD2 = []
        minibatchLossG2 = []
        minibatchLossD3 = []
        minibatchLossZreg = []
        FID = []
        
        if day is None:
            dayy = 'all'
        else:
            dayy = str(day)
            
        # noise variable
        noise = torch.FloatTensor(self.opts['batchSize'], self.nz, 1,1).to(self.device)
        
        if self.opts['do_pca']:
            Xpca = [] # will store some chunks
            pca_model = None
        
        # training loop
        for epoch in range(self.opts['nepochs']):

            for i, data in enumerate(traindataloader):
                
                data = data[0] # pytorch produces tuples by default
                data = data.view(data.size(0),1,data.size(1),data.size(2)).to(self.device)
                
                # map data X -> Z latent
                encoding = self.netE(data)
                # map Z -> Xhat
                reconstruction = self.netG(encoding)

                
                # map  Xhat -> class [0: Fake, 1: Real] (Make discriminator give it value 1)
                pred_rec_d1 = self.netD1(reconstruction.detach())
                # map X -> class (maximize D)
                pred_real_d1 = self.netD1(data)

                # For discriminator, the Pr(class=1|X) = 0.9, true_wp = label with that probability
                err_real_d1 = self.criterion_gan(pred_real_d1, true_wp(self.d_prob,pred_real_d1.size(),
                                                self.device))

                # For disc, probability this is a reconstruction, the Pr(class=1| Xhat) = 0.1 = d_noise
                err_fake_d1 = self.criterion_gan(pred_rec_d1, true_wp(1.-self.d_prob,pred_real_d1.size(),
                                                self.device))
                
                err_d1 = err_real_d1 + err_fake_d1

                self.optimizerD1.zero_grad()
                err_d1.backward()
                # minimize  -logD(X) and maximize -log(D(Xhat)) only w.r.t Discriminator params!
                self.optimizerD1.step()

                # map Xhat -> class
                pred_rec_d1 = self.netD1(reconstruction)
                labell = torch.FloatTensor(self.opts['batchSize'],1).fill_(1.).to(self.device) # true label
                errG_discrim = self.criterion_gan(pred_rec_d1, labell)
                errG_recon = (self.criterion_dist(reconstruction, data) + \
                              self.criterion_dist(self.downsample_pth(reconstruction),
                              self.downsample_pth(data))) * self.opts['lambdaa']

                err_g_d1 = errG_discrim + errG_recon

                self.optimizerG.zero_grad()
                err_g_d1.backward()
                # maximize log D(Xhat) or minimize -log D(Xhat) + MSE for encoder and generator
                self.optimizerG.step()

                # ------------- Diffusion step --------------- #

                encoding = self.netE(data)
                reconstruction = self.netG(encoding)
                pred_rec_d2 = self.netD2(reconstruction.detach())
                err_real_d2 = self.criterion_gan(pred_rec_d2, true_wp(self.d_prob,pred_real_d1.size(),self.device))

                
                if self.opts['noise_dist'] == 'normal':
                    noise.normal_(0., self.opts['z_var'])
                else:
                    noise.uniform_(-self.opts['z_var'],self.opts['z_var'])

                # Make Discriminator D2 learn difference between 
                fake = self.netG(noise)
                pred_fake_d2 = self.netD2(fake.detach())
                err_fake_d2 = self.criterion_gan(pred_fake_d2, true_wp(1.-self.d_prob,pred_real_d1.size(),self.device))
                err_d2 = err_real_d2 + err_fake_d2

                self.optimizerD2.zero_grad()
                err_d2.backward()
                self.optimizerD2.step()


                #------ extra regularization for z------#
                if self.opts['z_reg']:
                    self.optimizerG.zero_grad()
                    fake = self.netG(noise)
                    err_E = self.opts['zreg_weight'] * self.criterion_dist(self.netE(fake), noise.squeeze())
                    err_E.backward()
                    self.optimizerG.step()
                    
                pred_fake_d2 = self.netD2(fake.detach())
                # true label
                labell = torch.FloatTensor(self.opts['batchSize'],1).fill_(1.).to(self.device) 
                err_g_d2 = self.criterion_gan(pred_fake_d2, labell)

                self.optimizerD2.zero_grad()
                self.optimizerG.zero_grad()
                err_g_d2.backward()
                self.optimizerG.step()


                ######  FID SCORE AND INCEPTION NET ######
                pred_real_d3 = self.netD3(data)
                pred_fake_d3 = self.netD3(fake.detach())
                err_real_d3 = self.criterion_gan(pred_real_d3, true_wp(self.d_prob, pred_real_d3.size(),self.device))
                err_fake_d3 = self.criterion_gan(pred_fake_d3, true_wp(1.-self.d_prob, pred_fake_d3.size(), self.device))
                inception_loss = err_real_d3 + err_fake_d3

                self.optimizerD3.zero_grad()
                inception_loss.backward()
                self.optimizerD3.step()

                # compute fid score
                if self.opts['get_FID_score']:
                    with torch.no_grad():#

                        if self.opts['noise_dist'] == 'normal':
                            noise.normal_(0., self.opts['z_var'])
                        else:
                            noise.uniform_(-self.opts['z_var'],self.opts['z_var'])
                        fake = self.netG(noise)
                        fid = self.netD3.fid_score(data.detach(), fake.detach())
                        FID.append(fid)
                else:
                    FID.append(-1.)
                
                
                if self.opts['do_pca']:
                    if len(Xpca) < self.opts['npca_samples'] and pca_model is None:
                        data = data.detach().cpu().numpy().squeeze()
                        for d in data:
                            Xpca.append(d)
                        
                # SAVE LOSSES
                minibatchLossD1.append(float(err_d1.detach()))
                minibatchLossD2.append(float(err_d2.detach()))
                minibatchLossG1_gan.append(float(errG_discrim.detach()))
                minibatchLossG1_rec.append(float(errG_recon.detach()))
                minibatchLossG2.append(float(err_g_d2.detach()))
                minibatchLossD3.append(float(inception_loss.detach()))
                minibatchLossZreg.append(float(err_E.detach()))
                
                if i % self.opts['log_every'] == 0:
                    # LOG 
                    
                    print('[%d/%d][%d/%d] D1: %.2f D2: %.2f G1_gan: %.2f G1_rec: %.2f G2: %.2f D3: %.2f Zreg: %.2f, FID: %.3f'
                      % (epoch, self.opts['nepochs'], i, len(traindataloader),
                        np.mean(minibatchLossD1[-self.opts['log_every']:]),
                         np.mean(minibatchLossD2[-self.opts['log_every']:]),
                         np.mean(minibatchLossG1_gan[-self.opts['log_every']:]),
                         np.mean(minibatchLossG1_rec[-self.opts['log_every']:]),
                         np.mean(minibatchLossG2[-self.opts['log_every']:]),
                         np.mean(minibatchLossD3[-self.opts['log_every']:]),
                         np.mean(minibatchLossZreg[-self.opts['log_every']:]),
                         np.mean(FID[-self.opts['log_every']:]))
                      )

                    self.checkpoint(day, i, epoch)

                # END OF MINIBATCH
            
            if self.opts['lr_schedule']:
                self._scheduler_step()
                
            # END OF EPOCH
            # document losses at end of epoch
            losspath = join(self.outpath, 'net_training_losses')
            if not os.path.exists(losspath):
                os.makedirs(losspath)
            np.save(join(losspath, 'epoch'+str(epoch)+'_D1'),np.array(minibatchLossD1))
            np.save(join(losspath, 'epoch'+str(epoch)+'_D2'),np.array(minibatchLossD2))
            np.save(join(losspath, 'epoch'+str(epoch)+'_G1_rec'),np.array(minibatchLossG1_rec))
            np.save(join(losspath, 'epoch'+str(epoch)+'_G1_gan'),np.array(minibatchLossG1_gan))
            np.save(join(losspath, 'epoch'+str(epoch)+'_D2'),np.array(minibatchLossG2))
            np.save(join(losspath, 'epoch'+str(epoch)+'_D3'),np.array(minibatchLossD3))
            np.save(join(losspath, 'epoch'+str(epoch)+'_Zreg'),np.array(minibatchLossZreg))
            np.save(join(losspath, 'epoch'+str(epoch)+'_FID'),np.array(FID))
                    
                    
            # do checkpointing of models
            if epoch % opts['model_checkpoint_every']==0:
                
                torch.save(self.netG.state_dict(), '%s/netG_epoch_%d_day_%s.pth'%(self.outpath,epoch,
                                                                                    dayy))
                torch.save(self.netD1.state_dict(), '%s/netD1_epoch_%d_day_%s.pth' % (self.outpath,epoch,
                                                                                    dayy))
                torch.save(self.netD2.state_dict(), '%s/netD2_epoch_%d_day_%s.pth' % (self.outpath,epoch,
                                                                                    dayy))
                torch.save(self.netD3.state_dict(), '%s/netD3_epoch_%d_day_%s.pth' % (self.outpath,epoch,
                                                                                    dayy))
                torch.save(self.netE.state_dict(), '%s/netE_epoch_%d_day_%s.pth' % (self.outpath, epoch,
                                                                                    dayy))
            
            if self.opts['do_pca'] and pca_model is None:
                print('///// learning PCA model /////')
                pca_model = learn_pca_model(Xpca, self.opts['npca_components'], 
                                            random_state = self.opts['manualSeed'])
                print('///// PCA model learned, %d components /////'%(pca_model.n_components_))
                Xpca = []
                gc.collect()
                joblib.dump({'pca_model':pca_model}, join(self.outpath,'pca_model.pkl'))

            
    def checkpoint(self, day, minibatch_idx, epoch) -> None:
        
        # noise variable
        noise = torch.FloatTensor(self.opts['batchSize'], self.nz, 1,1).to(self.device)

        # sample and reconstruct
        with torch.no_grad():
            
            if self.opts['noise_dist'] == 'normal':
                noise.normal_(0., self.opts['z_var'])
            else:
                noise.uniform_(-self.opts['z_var'],self.opts['z_var'])
        

            # randomly sample a file and save audio sample
            sample = self.dataset.get(day, nsamps=1)[0] # first element of list output 
            
            # audio
            if self.opts['get_audio']:
                try:
                    save_audio_sample(lc.istft(inverse_transform(sample)), \
                                        '%s/input_audio_epoch_%03d_batchnumb_%d.wav' % 
                                        (self.outpath, epoch, minibatch_idx), self.opts['sample_rate'])
                except:
                    print('..audio buffer error, skipped audio file generation')

            # save original spectrogram
            gagan_save_spect('%s/input_spect_epoch_%03d_batchnumb_%d.png'
                                % (self.outpath, epoch, minibatch_idx), 
                                rescale_spectrogram(sample), frmat='png')
            # get reconstruction
            zvec = overlap_encode(sample, self.netE, transform_sample = False,
                                imageW = self.opts['imageW'],
                                noverlap = self.opts['noverlap'], cuda = self.opts['cuda'])

            spect, audio = overlap_decode(zvec, self.netG, noverlap = self.opts['noverlap'],
                                        get_audio = self.opts['get_audio'], 
                                        cuda = self.opts['cuda'])

            # save reconstructed spectrogram
            spect = rescale_spectrogram(spect)
            gagan_save_spect('%s/rec_spect_epoch_%03d_batchnumb_%d.png' % (self.outpath,
                            epoch, minibatch_idx), spect, frmat='png')
            
            if self.opts['get_audio']:
                try:
                    save_audio_sample(audio,'%s/rec_audio_epoch_%03d_batchnumb_%d.wav' % 
                                        (self.outpath, epoch, minibatch_idx), self.opts['sample_rate'])
                except:
                    print('..audio buffer error, skipped audio file generation')


    def get_loglikelihood(self, model, data, lengths, normalize_by_length=True):
        LogL = 0.
        for n in range(len(lengths)):
            ll = model.score(data[n]) 
            if normalize_by_length:
                ll /= lengths[n]
            LogL += ll
        return LogL / n

    def compute_latent_vectors(self, day):
        
        if self.X is None:
            self.X = self.dataset.get(day, nsamps=-1)
        
        if len(self.X) == 0:
            return 
        
        self.Z = [overlap_encode(x, self.netE, transform_sample=False,
                                imageW=self.opts['imageW'], 
                                noverlap = self.opts['noverlap'],
                                cuda=self.opts['cuda']) for x in self.X]
        # munge sequences
        if self.opts['munge']:
            self.Z = munge_sequences(self.Z, self.opts['munge_len'])
            
    def train_hmm(self, day, hidden_size):
        
        # encode all spectrograms from that day
        if self.Z is None:
            self.compute_latent_vectors(day)
        
        # split into train and validation
        ids = np.random.permutation(len(self.Z))
        ntrain = int(self.opts['hmm_train_proportion'] * len(ids))
        ztrain = [self.Z[ids[i]] for i in range(ntrain)]
        ztest = [self.Z[ids[i]] for i in range(ntrain, len(ids))]
        ids_train = ids[:ntrain]
        ids_test = ids[ntrain:]

        # get lengths of sequences
        Ltrain = [z.shape[0] for z in ztrain]
        
        # train HMM 
        print('\n..... learning hmm with %d states on %d data points .....'%(hidden_size, np.sum(Ltrain)))
        hmm = learnKmodels_getbest(ztrain, None, Ltrain, hidden_size, self.opts)
        
        if hmm is None:
            print('..... probably too few data points to fit, skipping .....')
            return
        
        # compute test log likelihood
        Ltest = [z.shape[0] for z in ztest]
        test_score = self.get_loglikelihood(hmm, ztest, Ltest, opts['normalize_logl'])
        print('..... test likelihood = %.4f .....'%(test_score))
        
        # compute train log likelihood
        train_score = self.get_loglikelihood(hmm, ztrain, Ltrain, opts['normalize_logl'])
        print('..... train likelihood = %.4f .....'%(train_score))

        # create 10 samples
        # concatenate the sequences because otherwise they are usually shorter than batch_size
        outputfolder = join(self.outpath, 'hmm_hiddensize_'+str(hidden_size))
        if not os.path.exists(outputfolder):
            os.makedirs(outputfolder)

        # choose some ztrain for saving
        inds_to_save = np.random.choice(len(ztrain), size=self.opts['nsamps'])
        ztosave = [ztrain[i] for i in inds_to_save]
        ztosave = np.concatenate(ztosave, axis=0)

        # save samples
        create_output(hmm, outputfolder, hidden_size, day, self.opts, self.netG, [])
        # save real files
        create_output(hmm, outputfolder, hidden_size, day, self.opts, self.netG, ztosave)
        print('..... generated samples and reconstruction .....')
        
        # save model
        joblib.dump({'train_score':train_score,
                     'test_score': test_score,
                     'ztrain':ztrain,'ztest':ztest,
                     'ids_train':ids_train,
                     'ids_test':ids_test,
                     'Lengths_train':Ltrain,
                     'Lengths_test':Ltest
                    },
                     join(outputfolder, 'data_and_scores_day_' + str(day) + '.pkl')
                   )
        
        joblib.dump({'model':hmm}, join(outputfolder, 'model_day_'+str(day)+'.pkl'))
        
    

        



parser = argparse.ArgumentParser()
parser.add_argument('--datapath', required=True, help='path to training dataset')
parser.add_argument('--birdname', type=str, required=True)
parser.add_argument('--outf', required=True, help='folder to output images and model checkpoints')
parser.add_argument('--chain_networks', action='store_true', help='whether to initialize the networks from networks on the previous day')
parser.add_argument('--batchSize', type=int, default=128, help='input batch size')
parser.add_argument('--nz', type=int, default=32, help='size of the latent z vector')
parser.add_argument('--train_residual', action = 'store_true')
parser.add_argument('--skip_network_training', action='store_true', help="if True, uses provided paths to networks and does not train")
parser.add_argument('--train_all_days', action = 'store_true', help='if True, trains single net on all spectrograms from all days')
parser.add_argument('--make_run_folder', action = 'store_true', help='if True, will create a run folder to save results')
parser.add_argument('--noise_dist', type=str, default = 'normal', help='noise distribution: {normal, uniform, t}')
parser.add_argument('--lambdaa', type = float, default = 100., help = 'weighting for recon loss')
parser.add_argument('--ngf', type=int, default=128,  help='num filter progression factor for generator')
parser.add_argument('--ndf', type=int, default=128, help='num filter progression factor for discriminator')
parser.add_argument('--nepochs', type=int, default=50, help='number of epochs to train GAN ')
parser.add_argument('--lr', type=float, default = 1e-5, help='learning rate')
parser.add_argument('--z_reg', action="store_true", help='whether to regularize the posterior')
parser.add_argument('--zreg_weight', type = float, default = 1., help = 'weight for z regularization')
parser.add_argument('--z_var', type = float, default = 1., help = 'variance of latent prior')
parser.add_argument('--manualSeed', type=int, default=-1, help='random number generator seed')
parser.add_argument('--lr_schedule', action = 'store_true', help='change learning rate')
parser.add_argument('--log_every', type=int, default=300, help='make images and print loss every X batches')
parser.add_argument('--model_checkpoint_every', type=int, default=5, help='save network weights every x epochs')
parser.add_argument('--get_FID_score', action='store_true', help='whether to compute FID score')
parser.add_argument('--do_pca', action='store_true', help='learn a PCA model if True')
parser.add_argument('--npca_components', type=float, default=0.98, help='percent variance to be explained by PCA')
parser.add_argument('--get_audio', action='store_true', help='write wav file from spectrogram')
parser.add_argument('--cuda', action='store_true', help='use gpu')
parser.add_argument('--start_from_day', type=int, default=0, help='which day to start learning from')
parser.add_argument('--end_at', type=int, default=-1, help='which day to end hmm learning at')
parser.add_argument('--netE',type = str, default = '', help='path to encoder network file')
parser.add_argument('--netG',type = str, default = '', help='path to generator network file')
parser.add_argument('--netD1',type = str, default = '', help='path to disc 1 network file')
parser.add_argument('--netD2',type = str, default = '', help='path to encoder network file')
parser.add_argument('--netD3',type = str, default = '', help='path to encoder network file')
parser.add_argument('--min_num_batches', type=int, default = 50, help='minimum number of minibatches')
# for HMM
parser.add_argument('--hidden_state_size', type = int, nargs = '+', default = [5, 10, 12, 15, 20, 25, 30, 40, 50, 60, 100])
parser.add_argument('--covariance_type', type = str, default = 'diag')
parser.add_argument('--covars_prior', type = float, default = 1., help ='diagnoal term weight on the prior covariance')
parser.add_argument('--fit_params', type = str, default = 'stmc',
                    help = 'which parameters to fit, s = startprob, t = transmat, m = means, c = covariances')
parser.add_argument('--transmat_prior', type = float, default = 1., help = 'transition matrix prior concentration')
parser.add_argument('--n_iter', type = int, default = 400, help = 'number of EM iterations')
parser.add_argument('--tolerance', type = float, default = 0.01)
parser.add_argument('--hmm_train_proportion', type=float, default=0.8, help='proportion of sequences to be used for hmm learning')
parser.add_argument('--min_seq_multiplier', type = int ,default = 5, help='the number of files should be at least hidden size x this factor')
parser.add_argument('--normalize_logl',action='store_true',help='if True, normalize the loglikelihood across time steps')
parser.add_argument('--init_params', type = str, default = 'kmeans',
                    help='which variables to initialize, or to initialize with kmeans, enter kmeans')
parser.add_argument('--munge', action = 'store_true', help='whether to concate')
parser.add_argument('--munge_len', type = int, default = 50, help = 'minimum length of a sequence to which sequences be concatenated')




def main():

    
    args = parser.parse_args()
    args  = vars(args)
    for k,v in args.items():
        if k in opts.keys():
            opts[k] = v


    if opts['manualSeed']==-1:
        opts['manualSeed'] = random.randint(1, 10000) # fix seed

    # fix seed
    print("Random Seed: ", opts['manualSeed'])
    random.seed(opts['manualSeed'])
    torch.manual_seed(opts['manualSeed'])
    if opts['cuda']:
        torch.cuda.manual_seed(opts['manualSeed'])
    opts['hmm_random_state'] = opts['manualSeed']

    # make bird dataset
    dataset = bird_dataset_single_hdf(opts['datapath'], opts['birdname'])
    # how many days are there ? 
    ndays = dataset.ndays
    if ndays==0:
        print('..... no data for this bird, exiting .....')
        return
    
    end_at = ndays
    if opts['end_at'] > 0:
        end_at = opts['end_at']
    
    
    # make model
    model = Model(dataset, '', opts, None)
    
    # make a save folder (run e.g. /opts['outf']/2021-09-10_15_12_31) if needed
    if opts['make_run_folder']:
        opts['outf'] = make_output_folder(opts['outf'])
    else:
        # just make the folder 
        if not os.path.exists(opts['outf']):
            os.makedirs(opts['outf'])
    # set output directory for run
    model.outpath = opts['outf']
        
    # save options dict
    with open(join(opts['outf'], 'opts.json'), 'w') as file:
        json.dump(opts, file)
        
        
    if not opts["skip_network_training"] and opts['train_all_days']:
        # train on all days from this bird
        traindataloader = model.make_single_dataloader()
        model.train_network(traindataloader)
        # get rid of rubbish
        traindataloader = None
        model.netD1 = None
        model.netD2 = None
        model.netD3 = None
        gc.collect()
        
        
    # train over days
    for day in range(opts['start_from_day'], end_at):
        
        print('\n\n.... ### WORKING ON DAY %d for bird %s ### .....'%(day, opts['birdname']))
        
        print(f'..... day name is {dataset.day_names[day]} .....')
        
        # update output folder
        outpath = join(opts['outf'], 'day_' + str(day))
        if not os.path.exists(outpath):
            os.makedirs(outpath, exist_ok=True)
            
        model.outpath = outpath

        # re-initialize networks
        if day > 0 and not opts["skip_network_training"] and not opts['chain_networks'] and not opts['train_all_days']:
            model._init_networks()

        
        if not opts["skip_network_training"] and not opts['train_all_days']:
            
            # make daily traindataloader
            traindataloader = model.make_daily_dataloader(day)
            N = len(model.X)
            print('..... %d sequences on this day .....'%(N))

            if len(traindataloader) < opts['min_num_batches']:
                print('..... NOT ENOUGH MINIBATCHES FOR NEURAL NET .....')
                continue

            # train networks
            model.train_network(traindataloader, day)
         
        
        ##### HMM LEARNING #####
        # compute and encode spectrograms 
        model.compute_latent_vectors(day)
        
        if len(model.X) == 0:
            print('..... no data on this day, skipping .....')
            continue
            
        # get total number of data points in latent space
        tot_pts = np.sum([z.shape[0] for z in model.Z])
        
        # then, train hmms, one per given hidden state size
        for k in range(len(opts['hidden_state_size'])):
            
            K = opts['hidden_state_size'][k]
            num_params = hmm_num_free_params(K, opts['nz'], covariance_type=opts['covariance_type'])
            
            if tot_pts < num_params:
                print(f'..... too few data points to learn an hmm with {K} states, skipping to next.....')
                continue
                
            model.train_hmm(day, K)

        # clear the saved spectrogram and latent vectors arrays
        model.X = None 
        model.Z = None
        gc.collect()
        torch.cuda.empty_cache()
        
    
    # last is tutor
    print('..... now learning tutor model .....')
     
    
    for k in range(len(opts['hidden_state_size'])):
            
        K = opts['hidden_state_size'][k]
        num_params = hmm_num_free_params(K, opts['nz'], covariance_type=opts['covariance_type'])

        if tot_pts < num_params:
            print(f'..... too few data points to learn an hmm with {K} states, skipping to next.....')
            continue
            
        model.train_hmm('tutor', K)
        

    
    model.dataset.close()
   


    
if __name__ == '__main__':
    main()