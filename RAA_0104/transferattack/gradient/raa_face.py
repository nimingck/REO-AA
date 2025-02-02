import torch

from ..utils import *
from ..attack import Attack
import torch.nn.functional as F
from torchvision import transforms


class RAA(object):
    # with face
    def __init__(self, model_name, epsilon=16/255, alpha=1.6/255, epoch=100, decay=1., targeted=False, random_start=False,
                norm='linfty', loss='pairwise', device="cuda:0", attack='RAA', **kwargs):
        self.n_theta = 25  # Number of samples
        self.mu = 0.3  # Learning rate for sampling
        self.device = device
        self.model = model_name.to(self.device)
        self.eps_delta = 16/255
        self.eps_theta = 16/255
        self.labda = 0.06
        self.targeted = targeted
        self.step = 100
        self.lr = 1.6/255
        self.img_max, self.img_min = 1., 0
        self.alpha = alpha
        self.epoch = epoch
        self.decay = decay
        self.random_start = random_start
        self.norm = norm

    def load_model(self, model_name):
        """
        The model Loading stage, which should be overridden when surrogate model is customized (e.g., DSM, SETR, etc.)
        Prioritize the model in torchvision.models, then timm.models

        Arguments:
            model_name (str/list): the name of surrogate model in model_list in utils.py

        Returns:
            model (torch.nn.Module): the surrogate model wrapped by wrap_model in utils.py
        """
        def load_single_model(model_name):
            if model_name in models.__dict__.keys():
                print('=> Loading model {} from torchvision.models'.format(model_name))
                model = models.__dict__[model_name](weights="DEFAULT")
            elif model_name in timm.list_models():
                print('=> Loading model {} from timm.models'.format(model_name))
                model = timm.create_model(model_name, pretrained=True)
            else:
                raise ValueError('Model {} not supported'.format(model_name))
            return wrap_model(model.eval().cuda())

        if isinstance(model_name, list):
            return EnsembleModel([load_single_model(name) for name in model_name])
        else:
            return load_single_model(model_name)
        
    def feat_forward(self, x, x_pre):
        """
        :param x: Inputs to perturb
        :param y: Ground-truth label
        :return adversarial image
        """
        x = transforms.ToTensor()(x)
        x = x.to(self.device)

        # 1. Initialize delta and constrain it
        delta = torch.rand_like(x)
        delta = torch.clamp(delta, min=-self.eps_delta, max=self.eps_delta).to(self.device)
        
        for i in range(self.step):
            x_adv = x + delta
            # k = F.cross_entropy(self.model(x_adv), self.target, reduce=False, reduction="sum")
            # print(torch.mean(k))
            loss_print = 0
            self.model.eval()
            self.model.zero_grad()
            sum_direction = torch.zeros_like(delta)
            
            for _ in range(self.n_theta):
                # 2. Sample n different thetas from a Gaussian distribution and constrain them
                theta = self.labda*torch.randn_like(x_adv).to(self.device)
                #theta = torch.clamp(theta, min=-self.eps_theta, max=self.eps_theta)

                # 3. Compute L_theta for each theta
                theta.requires_grad = True
                x_repeated = (x_adv+theta).unsqueeze(0)
                x_pre = torch.tensor(x_pre).to(self.device)
                loss_theta = F.pairwise_distance(self.model(self.transform(x_repeated)),x_pre)
                loss_theta = torch.mean(loss_theta)
            
                # 4. Update thetanew and constrain it
                loss_theta.backward(retain_graph=True)
                grad_theta = theta.grad.sign().detach()
                if(self.targeted):
                    thetanew = (0.05*theta + self.mu * grad_theta).unsqueeze(0)
                else:
                    thetanew = (0.05*theta - self.mu * grad_theta).unsqueeze(0)

                # 5. Compute direction_theta = L(x + delta + thetanew) * theta
                x_repeated_loss = x_adv.unsqueeze(0)
                loss = F.pairwise_distance(self.model(x_repeated_loss),x_pre)
                # loss = F.cross_entropy(self.model(x_adv+thetanew), target, reduce=False, reduction="sum")
                # loss_mean = torch.mean(loss)
                if(self.targeted):
                    direction_theta = loss.unsqueeze(1).unsqueeze(2).unsqueeze(3).expand(-1, thetanew.size(1), thetanew.size(2), thetanew.size(3)) * thetanew
                else:      
                    direction_theta = -loss.unsqueeze(1).unsqueeze(2).unsqueeze(3).expand(-1, thetanew.size(1), thetanew.size(2), thetanew.size(3)) * thetanew
                # 6. Accumulate the direction
                if(len(sum_direction)==3):
                    sum_direction = sum_direction.unsqueeze(0)
                sum_direction += direction_theta.detach()

                loss_print += loss_theta
            
            # 7. Update delta
            if(self.targeted):
                delta = delta - (sum_direction / self.n_theta).sign() * self.lr
            else:
                delta = delta + (sum_direction / self.n_theta).sign() * self.lr
            # delta = delta - (sum_direction / self.n_theta)*0.01
            
            # 8. Constrain delta
            delta = torch.clamp(delta, min=-self.eps_delta, max=self.eps_delta)
            delta = delta.squeeze(0)
            if i % 20 == 0:
                print("step: {}".format(i), "loss: {}".format((loss_print/self.n_theta).detach()))

        delta = clamp(delta, self.img_min-x, self.img_max-x)

        return delta.unsqueeze(0), self.model
    
    def transform(self, data, **kwargs):
        return data