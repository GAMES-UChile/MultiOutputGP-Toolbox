import sys
import math
import torch
import numpy as np
from IPython.display import display, HTML
from . import Parameter, Mean, Kernel, MultiOutputKernel, Likelihood, MultiOutputLikelihood, GaussianLikelihood, config, plot_gram

def init_inducing_points(Z, X, kernel):
    # TODO: instead of linspace, use quantile space or k-means to initialize positions
    output_dims = kernel.output_dims
    if 1 < output_dims:
        if isinstance(Z, int) or all(isinstance(z, int) for z in Z) and len(Z) == output_dims:
            if isinstance(Z, int):
                Z = [Z] * output_dims
            M = Z
            Z = torch.zeros((sum(M),X.shape[1]))
            for j in range(len(M)):
                m0 = sum(M[:j])
                m = M[j]
                Z[m0:m0+m,0] = j
                for i in range(X.shape[1])[1:]:
                    x = X[X[:,0] == j,:]
                    Z[m0:m0+m,i] = torch.linspace(torch.min(x[:,i]), torch.max(x[:,i]), m)
    elif isinstance(Z, int):
        M = Z
        Z = torch.zeros((M,X.shape[1]))
        # TODO: this puts values "diagonal", better to use grid?
        for i in range(X.shape[1]):
            Z[:,i] = torch.linspace(torch.min(X[:,i]), torch.max(X[:,i]), M)
    return Z

class CholeskyException(Exception):
    def __init__(self, message, K, model):
        self.message = message
        self.K = K
        self.model = model

    def __str__(self):
        return self.message

class Model:
    """
    Base model class.
    """
    def __init__(self, kernel, X, y, likelihood=GaussianLikelihood(1.0), jitter=1e-8, mean=None, name=None):
        if not issubclass(type(kernel), Kernel):
            raise ValueError("kernel must derive from mogptk.gpr.Kernel")
        X, y = self._check_input(X, y)
        if mean is not None:
            if not issubclass(type(mean), Mean):
                raise ValueError("mean must derive from mogptk.gpr.Mean")
            mu = mean(X).reshape(-1,1)
            if mu.shape != y.shape:
                raise ValueError("mean and y data must match shapes: %s != %s" % (mu.shape, y.shape))

        if issubclass(type(likelihood), MultiOutputLikelihood) and likelihood.output_dims != kernel.output_dims:
            raise ValueError("kernel and likelihood must have matching output dimensions")
        likelihood.validate_y(y, X=X)

        # limit to number of significant digits
        if config.dtype == torch.float32:
            jitter = max(jitter, 1e-6)
        elif config.dtype == torch.float64:
            jitter = max(jitter, 1e-15)

        self.kernel = kernel
        self.X = X
        self.y = y
        self.likelihood = likelihood
        self.jitter = jitter
        self.mean = mean
        self.name = name
        self.input_dims = X.shape[1]

        self._params = []
        self._register_parameters(kernel)
        self._register_parameters(likelihood)
        if mean is not None and issubclass(type(mean), Mean):
            self._register_parameters(mean)

    def __setattr__(self, name, val):
        if hasattr(self, name) and isinstance(getattr(self, name), Parameter):
            raise AttributeError("parameter is read-only, use Parameter.assign()")
        super().__setattr__(name, val)        

    def _check_input(self, X, y=None):
        if not isinstance(X, torch.Tensor):
            X = torch.tensor(X, device=config.device, dtype=config.dtype)
        else:
            X = X.to(config.device, config.dtype)
        if X.ndim == 0:
            X = X.reshape(1,1)
        elif X.ndim == 1:
            X = X.reshape(-1,1)
        elif X.ndim != 2:
            raise ValueError("X must have dimensions (data_points,input_dims) with input_dims optional")
        if X.shape[0] == 0 or X.shape[1] == 0:
            raise ValueError("X must not be empty")

        if y is not None:
            if not isinstance(y, torch.Tensor):
                y = torch.tensor(y, device=config.device, dtype=config.dtype)
            else:
                y = y.to(config.device, config.dtype)
            if y.ndim == 0:
                y = y.reshape(1,1)
            elif y.ndim == 1:
                y = y.reshape(-1,1)
            elif y.ndim != 2 or y.shape[1] != 1:
                raise ValueError("y must have one dimension (data_points,)")
            if X.shape[0] != y.shape[0]:
                raise ValueError("number of data points for X and y must match")
            return X, y
        else:
            # X is for prediction
            if X.shape[1] != self.input_dims:
                raise ValueError("X must have %s input dimensions" % self.input_dims)
            return X

    def _index_channel(self, value, X):
        if 1 < self.kernel.output_dims and 0 < value.ndim and value.shape[0] == self.kernel.output_dims:
            return torch.index_select(value, dim=0, index=X[:,0].long())
        return value

    def _register_parameters(self, obj, name=None):
        if isinstance(obj, Parameter):
            if obj.name is not None:
                if name is None:
                    name = obj.name
                else:
                    name += "." + obj.name
            elif name is None:
                name = ""
            obj.name = name
            self._params.append(obj)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                self._register_parameters(v, (name if name is not None else "")+"["+str(i)+"]")
        elif issubclass(type(obj), (Kernel, Mean, Likelihood)):
            for v in obj.__dict__.values():
                self._register_parameters(v, (name+"." if name is not None else "")+obj.name)

    def zero_grad(self):
        for p in self._params:
            p = p.unconstrained
            if p.grad is not None:
                if p.grad.grad_fn is not None:
                    p.grad.detach_()
                else:
                    p.grad.requires_grad_(False)
                p.grad.zero_()

    def parameters(self):
        """
        Yield trainable parameters of model.

        Returns:
            Parameter generator
        """
        for p in self._params:
            if p.trainable:
                yield p.unconstrained

    def get_parameters(self):
        """
        Return all parameters of model.

        Returns:
            list: List of Parameters.
        """
        return self._params
    
    def print_parameters(self, file=None):
        """
        Print parameters and their values.
        """
        def param_range(lower, upper, trainable=True, pegged=False):
            if lower is not None:
                if math.prod(lower.shape) == 1:
                    lower = lower.item()
                else:
                    lower = lower.tolist()
            if upper is not None:
                if math.prod(upper.shape) == 1:
                    upper = upper.item()
                else:
                    upper = upper.tolist()

            if pegged:
                return "pegged"
            elif not trainable:
                return "fixed"
            if lower is None and upper is None:
                return "(-∞, ∞)"
            elif lower is None:
                return "(-∞, %s]" % upper
            elif upper is None:
                return "[%s, ∞)" % lower
            return "[%s, %s]" % (lower, upper)

        if file is None:
            try:
                get_ipython  # fails if we're not in a notebook
                table = '<table><tr><th style="text-align:left">Name</th><th>Range</th><th>Value</th></tr>'
                for p in self._params:
                    table += '<tr><td style="text-align:left">%s</td><td>%s</td><td>%s</td></tr>' % (p.name, param_range(p.lower, p.upper, p.trainable, p.pegged), p.numpy())
                table += '</table>'
                display(HTML(table))
                return
            except Exception as e:
                pass

        vals = [["Name", "Range", "Value"]]
        for p in self._params:
            vals.append([p.name, param_range(p.lower, p.upper, p.trainable, p.pegged), str(p.numpy())])

        nameWidth = max([len(val[0]) for val in vals])
        rangeWidth = max([len(val[1]) for val in vals])
        for val in vals:
            print("%-*s  %-*s  %s" % (nameWidth, val[0], rangeWidth, val[1], val[2]), file=file)

    def _cholesky(self, K, add_jitter=False):
        if add_jitter:
            K += (self.jitter * K.diagonal().mean()).repeat(K.shape[0]).diagflat()
        try:
            return torch.linalg.cholesky(K)
        except RuntimeError as e:
            print("ERROR:", e.args[0], file=sys.__stdout__)
            if K.isnan().any():
                print("ERROR: kernel matrix has NaNs!", file=sys.__stdout__)
            if K.isinf().any():
                print("ERROR: kernel matrix has infinities!", file=sys.__stdout__)
            self.print_parameters()
            plot_gram(K)
            raise CholeskyException(e.args[0], K, self)

    def log_marginal_likelihood(self):
        """
        Return the log marginal likelihood given by

        $$ \\log p(y) $$

        Returns:
            torch.tensor: Log marginal likelihood.
        """
        raise NotImplementedError()

    def log_prior(self):
        """
        Return the log prior given by

        $$ \\log p(\\theta) $$

        Returns:
            torch.tensor: Log prior.
        """
        return sum([p.log_prior() for p in self._params])

    def loss(self):
        """
        Model loss for training.

        Returns:
            torch.tensor: Loss.
        """
        self.zero_grad()
        loss = -self.log_marginal_likelihood() - self.log_prior()
        loss.backward()
        return loss

    def K(self, X1, X2=None):
        """
        Evaluate kernel at `X1` and `X2` and return the NumPy representation.

        Args:
            X1 (torch.tensor): Input of shape (data_points0,input_dims).
            X2 (torch.tensor): Input of shape (data_points1,input_dims).

        Returns:
            numpy.ndarray: Kernel matrix of shape (data_points0,data_points1).
        """
        with torch.no_grad():
            return self.kernel(X1, X2).detach().cpu().numpy()

    #def quantile(self, p, mu, var):
    #    with torch.no_grad():
    #        if not isinstance(p, torch.Tensor):
    #            p = torch.tensor(p, device=config.device, dtype=config.dtype)
    #        else:
    #            p = p.to(config.device, config.dtype)
    #        p = p.squeeze()
    #        if p.ndim != 0:
    #            raise ValueError("quantile must be scalar")
    #        return self.likelihood.quantile(p, mu, var)

    def sample(self, Z, n=None, predict_y=True):
        """
        Sample from model.

        Args:
            Z (torch.tensor): Input of shape (data_points,input_dims).
            n (int): Number of samples.
            predict_y (boolean): Predict the data values \\(y\\) instead of the function values \\(f\\).

        Returns:
            torch.tensor: Samples of shape (data_points,samples) or (data_points,) if `n` is not given.
        """
        with torch.no_grad():
            S = n
            if n is None:
                S = 1

            # TODO: predict_y and non-Gaussian likelihoods
            mu, var = self.predict(Z, full=True, tensor=True, predict_y=predict_y)  # MxD and MxMxD
            eye = torch.eye(var.shape[0], device=config.device, dtype=config.dtype)
            var += self.jitter * var.diagonal().mean() * eye  # MxM

            u = torch.normal(
                    torch.zeros(Z.shape[0], S, device=config.device, dtype=config.dtype),
                    torch.tensor(1.0, device=config.device, dtype=config.dtype))  # MxS
            L = torch.linalg.cholesky(var)  # MxM
            samples = mu + L.mm(u)  # MxS

            if n is None:
                samples = samples.squeeze()
            return samples.cpu().numpy()

class Exact(Model):
    """
    Regular Gaussian process regression with a Gaussian likelihood which allows for exact inference.

    $$ y \\sim \\mathcal{N}(0, K + \\sigma^2I) $$

    Args:
        kernel (mpgptk.gpr.kernel.Kernel): Kernel.
        X (torch.tensor): Input data of shape (data_points,input_dims).
        y (torch.tensor): Output data of shape (data_points,).
        variance (float,torch.tensor): Gaussian likelihood initial variance. Passing a float will train a single variance for all channels. Passing a tensor of shape (channels,) will assign and train different variances per multi-output channel. Passing a tensor of shape (data_points,1) will assign and fix different variances per data point.
        jitter (float): Relative jitter of the diagonal's mean added to the kernel's diagonal before calculating the Cholesky.
        mean (mogptk.gpr.mean.Mean): Mean.
        name (str): Name of the model.
    """
    def __init__(self, kernel, X, y, variance=1.0, jitter=1e-8, mean=None, name="Exact"):
        self.variance_per_data = False
        variance = Parameter.to_tensor(variance)
        if variance.ndim == 2 and (X.ndim != 1 or variance.shape[0] == X.shape[0]) and variance.shape[1] == 1:
            self.variance_per_data = True
            variance = variance.reshape(-1)
        elif 1 < variance.ndim or variance.ndim == 1 and variance.shape[0] != kernel.output_dims:
            raise ValueError("variance must be float or have shape (channels,) or (data_points,1)")

        super().__init__(kernel, X, y, GaussianLikelihood(variance), jitter, mean, name)
        self.likelihood.scale.trainable = not self.variance_per_data

        self.eye = torch.eye(self.X.shape[0], device=config.device, dtype=config.dtype)
        self.log_marginal_likelihood_constant = 0.5*self.X.shape[0]*np.log(2.0*np.pi)

    def log_marginal_likelihood(self):
        Kff = self.kernel.K(self.X)
        if self.variance_per_data:
            Kff += self.likelihood.scale().diagflat()
        else:
            Kff += self._index_channel(self.likelihood.scale(), self.X) * self.eye  # NxN
        L = self._cholesky(Kff, add_jitter=True)  # NxN

        if self.mean is not None:
            y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
        else:
            y = self.y  # Nx1

        p = -self.log_marginal_likelihood_constant
        p -= L.diagonal().log().sum() # 0.5 is taken inside the log: L is the square root
        p -= 0.5*y.T.mm(torch.cholesky_solve(y,L)).squeeze()
        return p

    def predict(self, Xs, full=False, tensor=False, predict_y=True):
        if self.variance_per_data and predict_y:
            raise ValueError("can only predict function values when data point variances are given, set predict_y=False")

        with torch.no_grad():
            Xs = self._check_input(Xs)  # MxD
            if self.mean is not None:
                y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
            else:
                y = self.y  # Nx1

            Kff = self.kernel.K(self.X)
            if self.variance_per_data:
                Kff += self.likelihood.scale().diagflat()
            else:
                Kff += self._index_channel(self.likelihood.scale(), self.X) * self.eye  # NxN
            Kfs = self.kernel.K(self.X,Xs)  # NxM

            Lff = self._cholesky(Kff, add_jitter=True)  # NxN
            v = torch.linalg.solve_triangular(Lff,Kfs,upper=False)  # NxM

            mu = Kfs.T.mm(torch.cholesky_solve(y,Lff))  # Mx1
            if self.mean is not None:
                mu += self.mean(Xs).reshape(-1,1)  # Mx1

            if full:
                Kss = self.kernel.K(Xs)  # MxM
                var = Kss - v.T.mm(v)  # MxM
                if predict_y:
                    eye = torch.eye(var.shape[0], device=config.device, dtype=config.dtype)
                    var += self._index_channel(self.likelihood.scale(), Xs) * eye
            else:
                Kss_diag = self.kernel.K_diag(Xs)  # M
                var = Kss_diag - v.T.square().sum(dim=1)  # M
                if predict_y:
                    var += self._index_channel(self.likelihood.scale(), Xs)
                var = var.reshape(-1,1)

            if tensor:
                return mu, var
            else:
                return mu.cpu().numpy(), var.cpu().numpy()

class Snelson(Model):
    """
    A sparse Gaussian process regression based on Snelson and Ghahramani [1] with a Gaussian likelihood and inducing points.

    Args:
        kernel (mogptk.gpr.kernel.Kernel): Kernel.
        X (torch.tensor): Input data of shape (data_points,input_dims).
        y (torch.tensor): Output data of shape (data_points,).
        Z (int,torch.tensor): Number of inducing points to be distributed over the input space. Passing a tensor of shape (inducing_points,input_dims) sets the initial positions of the inducing points.
        variance (float,torch.tensor): Gaussian likelihood initial variance. Passing a float will train a single variance for all channels. Passing a tensor of shape (channels,) will assign and train different variances per multi-output channel.
        jitter (float): Relative jitter of the diagonal's mean added to the kernel's diagonal before calculating the Cholesky.
        mean (mogptk.gpr.mean.Mean): Mean.
        name (str): Name of the model.

    [1] E. Snelson, Z. Ghahramani, "Sparse Gaussian Processes using Pseudo-inputs", 2005
    """
    def __init__(self, kernel, X, y, Z=10, variance=1.0, jitter=1e-8, mean=None, name="Snelson"):
        variance = Parameter.to_tensor(variance).squeeze()
        if 1 < variance.ndim or variance.ndim == 1 and variance.shape[0] != kernel.output_dims:
            raise ValueError("variance must be float or have shape (channels,)")

        super().__init__(kernel, X, y, GaussianLikelihood(variance), jitter, mean, name)

        Z = init_inducing_points(Z, self.X, kernel)
        Z = self._check_input(Z)
        
        self.eye = torch.eye(Z.shape[0], device=config.device, dtype=config.dtype)
        self.log_marginal_likelihood_constant = 0.5*self.X.shape[0]*np.log(2.0*np.pi)
        self.Z = Parameter(Z, name="induction_points")
        if 1 < kernel.output_dims:
            self.Z.num_parameters -= self.Z().shape[0]

        self._register_parameters(self.Z)

    def log_marginal_likelihood(self):
        if self.mean is not None:
            y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
        else:
            y = self.y  # Nx1

        Kff_diag = self.kernel.K_diag(self.X)  # N
        Kuf = self.kernel.K(self.Z(),self.X)  # MxN
        Kuu = self.kernel.K(self.Z())  # MxM

        Luu = self._cholesky(Kuu, add_jitter=True)  # MxM;  Luu = Kuu^(1/2)
        v = torch.linalg.solve_triangular(Luu,Kuf,upper=False)  # MxN;  Kuu^(-1/2).Kuf
        g = Kff_diag - v.T.square().sum(dim=1) + self._index_channel(self.likelihood.scale(), self.X)  # N;  diag(Kff-Qff) + sigma^2.I
        G = torch.diagflat(1.0/g)  # N
        L = self._cholesky(v.mm(G).mm(v.T) + self.eye)  # MxM;  (Kuu^(-1/2).Kuf.G.Kfu.Kuu^(-1/2) + I)^(1/2)

        c = torch.linalg.solve_triangular(L,v.mm(G).mm(y),upper=False)  # Mx1;  L^(-1).Kuu^(-1/2).Kuf.G.y

        p = -self.log_marginal_likelihood_constant
        p -= L.diagonal().log().sum() # 0.5 is taken as the square root of L
        p -= 0.5*g.log().sum()
        p -= 0.5*y.T.mm(G).mm(y).squeeze()
        p += 0.5*c.T.mm(c).squeeze()
        return p

    def predict(self, Xs, full=False, tensor=False, predict_y=True):
        with torch.no_grad():
            Xs = self._check_input(Xs)  # MxD
            if self.mean is not None:
                y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
            else:
                y = self.y  # Nx1

            Kff_diag = self.kernel.K_diag(self.X)  # N
            Kuf = self.kernel.K(self.Z(),self.X)  # MxN
            Kuu = self.kernel.K(self.Z())  # MxM
            Kus = self.kernel.K(self.Z(),Xs)  # MxS

            Luu = self._cholesky(Kuu, add_jitter=True)  # MxM;  Kuu^(1/2)
            v = torch.linalg.solve_triangular(Luu,Kuf,upper=False)  # MxN;  Kuu^(-1/2).Kuf
            g = Kff_diag - v.T.square().sum(dim=1) + self._index_channel(self.likelihood.scale(), self.X)
            G = torch.diagflat(1.0/g)  # N
            L = self._cholesky(v.mm(G).mm(v.T) + self.eye)  # MxM;  (Kuu^(-1/2).Kuf.G.Kfu.Kuu^(-1/2) + I)^(1/2)

            a = torch.linalg.solve_triangular(Luu,Kus,upper=False)  # NxM
            b = torch.linalg.solve_triangular(L,a,upper=False)
            c = torch.linalg.solve_triangular(L,v.mm(G).mm(y),upper=False)  # Mx1;  L^(-1).Kuu^(-1/2).Kuf.G.y

            mu = b.T.mm(c)  # Mx1
            if self.mean is not None:
                mu += self.mean(Xs).reshape(-1,1)  # Mx1

            if full:
                Kss = self.kernel(Xs)  # MxM
                var = Kss - a.T.mm(w) + b.T.mm(u)  # MxM
                if predict_y:
                    eye = torch.eye(var.shape[0], device=config.device, dtype=config.dtype)
                    var += self._select_channel(self.likelihood.scale(), Xs) * eye
            else:
                Kss_diag = self.kernel.K_diag(Xs)  # M
                var = Kss_diag - a.T.square().sum(dim=1) + b.T.square().sum(dim=1)  # M
                if predict_y:
                    var += self._index_channel(self.likelihood.scale(), Xs)
                var = var.reshape(-1,1)

            if tensor:
                return mu, var
            else:
                return mu.cpu().numpy(), var.cpu().numpy()

class OpperArchambeau(Model):
    """
    A Gaussian process regression based on Opper and Archambeau [1] with a non-Gaussian likelihood.

    Args:
        kernel (mogptk.gpr.kernel.Kernel): Kernel.
        X (torch.tensor): Input data of shape (data_points,input_dims).
        y (torch.tensor): Output data of shape (data_points,).
        likelihood (mogptk.gpr.likelihood.Likelihood): Likelihood.
        jitter (float): Relative jitter of the diagonal's mean added to the kernel's diagonal before calculating the Cholesky.
        mean (mogptk.gpr.mean.Mean): Mean.
        name (str): Name of the model.

    [1] M. Opper, C. Archambeau, "The Variational Gaussian Approximation Revisited", 2009
    """
    def __init__(self, kernel, X, y, likelihood=GaussianLikelihood(variance=1.0),
                 jitter=1e-8, mean=None, name="OpperArchambeau"):
        super().__init__(kernel, X, y, likelihood, jitter, mean, name)

        n = self.X.shape[0]
        self.eye = torch.eye(n, device=config.device, dtype=config.dtype)
        self.q_nu = Parameter(torch.zeros(n,1), name="q_nu")
        self.q_lambda = Parameter(torch.ones(n,1), name="q_lambda", lower=config.positive_minimum)
        self.likelihood = likelihood

        self._register_parameters(self.q_nu)
        self._register_parameters(self.q_lambda)

    def elbo(self):
        if self.mean is not None:
            y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
        else:
            y = self.y  # Nx1

        q_nu = self.q_nu()
        q_lambda = self.q_lambda()

        Kff = self.kernel(self.X)  # NxN
        L = self._cholesky(q_lambda*q_lambda.T*Kff + self.eye)
        invL = torch.linalg.solve_triangular(L,self.eye,upper=False)  # NxN

        qf_mu = Kff.mm(q_nu)
        qf_var_diag = 1.0/q_lambda.square() - (invL.T.mm(invL)/q_lambda/q_lambda.T).diagonal().reshape(-1,1)

        kl = -q_nu.shape[0]
        kl += q_nu.T.mm(qf_mu).squeeze()  # Mahalanobis
        kl += L.diagonal().square().log().sum()  # determinant TODO: is this correct?
        #kl += invL.diagonal().square().sum()  # trace
        kl += invL.square().sum()  # trace

        if self.mean is not None:
            qf_mu = qf_mu - self.mean(self.X).reshape(-1,1)  # Sx1
        var_exp = self.likelihood.variational_expectation(y, qf_mu, qf_var_diag, X=self.X)

        #eye = torch.eye(q_lambda.shape[0], device=config.device, dtype=config.dtype)
        #qf_var = (1.0/q_lambda.square())*eye - invL.T.mm(invL)/q_lambda/q_lambda.T
        #kl = -q_nu.shape[0]
        #kl += q_nu.T.mm(qf_mu).squeeze()  # Mahalanobis
        #kl -= qf_var.det().log()  # determinant
        #kl += invL.diagonal().square().sum()  # trace

        #kl = -q_nu.shape[0]
        #kl += q_nu.T.mm(q_nu).squeeze()  # Mahalanobis
        #kl -= qf_var.det().log()  # determinant
        #kl += qf_var_diag.sum()  # trace
        return var_exp - 0.5*kl

    def log_marginal_likelihood(self):
        # maximize the lower bound
        return self.elbo()

    def predict(self, Xs, full=False, tensor=False, predict_y=True):
        with torch.no_grad():
            Xs = self._check_input(Xs)  # MxD

            Kff = self.kernel(self.X)
            Kfs = self.kernel(self.X,Xs)  # NxS

            L = self._cholesky(Kff + (1.0/self.q_lambda().square()).diagflat())  # NxN
            a = torch.linalg.solve_triangular(L,Kfs,upper=False)  # NxS;  Kuu^(-1/2).Kus

            mu = Kfs.T.mm(self.q_nu())  # Sx1
            if self.mean is not None:
                mu += self.mean(Xs).reshape(-1,1)  # Sx1

            if full:
                Kss = self.kernel(Xs)  # SxS
                var = Kss - a.T.mm(a)  # SxS
            else:
                Kss_diag = self.kernel.K_diag(Xs)  # M
                var = Kss_diag - a.T.square().sum(dim=1)  # M
                var = var.reshape(-1,1)

            if predict_y:
                mu, var = self.likelihood.predict(mu, var, full=full, X=Xs)

            if tensor:
                return mu, var
            else:
                return mu.cpu().numpy(), var.cpu().numpy()

class Titsias(Model):
    """
    A sparse Gaussian process regression based on Titsias [1] with a Gaussian likelihood.

    Args:
        kernel (mogptk.gpr.kernel.Kernel): Kernel.
        X (torch.tensor): Input data of shape (data_points,input_dims).
        y (torch.tensor): Output data of shape (data_points,).
        Z (int,torch.tensor): Number of inducing points to be distributed over the input space. Passing a tensor of shape (inducing_points,input_dims) sets the initial positions of the inducing points.
        variance (float): Gaussian likelihood initial variance.
        jitter (float): Relative jitter of the diagonal's mean added to the kernel's diagonal before calculating the Cholesky.
        mean (mogptk.gpr.mean.Mean): Mean.
        name (str): Name of the model.

    [1] Titsias, "Variational learning of induced variables in sparse Gaussian processes", 2009
    """
    # See: http://krasserm.github.io/2020/12/12/gaussian-processes-sparse/
    def __init__(self, kernel, X, y, Z, variance=1.0, jitter=1e-8,
                 mean=None, name="Titsias"):
        # TODO: variance per channel
        super().__init__(kernel, X, y, GaussianLikelihood(variance), jitter, mean, name)

        Z = init_inducing_points(Z, self.X, kernel)
        Z = self._check_input(Z)

        self.eye = torch.eye(Z.shape[0], device=config.device, dtype=config.dtype)
        self.log_marginal_likelihood_constant = 0.5*self.X.shape[0]*np.log(2.0*np.pi)
        self.Z = Parameter(Z, name="induction_points")
        if 1 < kernel.output_dims:
            self.Z.num_parameters -= self.Z().shape[0]

        self._register_parameters(self.Z)

    def elbo(self):
        if self.mean is not None:
            y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
        else:
            y = self.y  # Nx1

        Kff_diag = self.kernel.K_diag(self.X)  # N
        Kuf = self.kernel(self.Z(),self.X)  # MxN
        Kuu = self.kernel(self.Z())  # MxM

        Luu = self._cholesky(Kuu, add_jitter=True)  # MxM;  Kuu^(1/2)
        v = torch.linalg.solve_triangular(Luu,Kuf,upper=False)  # MxN;  Kuu^(-1/2).Kuf
        Q = v.mm(v.T)  # MxM;  Kuu^(-1/2).Kuf.Kfu.Kuu^(-1/2)
        L = self._cholesky(Q/self.likelihood.scale() + self.eye)  # MxM;  (Q/sigma^2 + I)^(1/2)

        c = torch.linalg.solve_triangular(L,v.mm(y),upper=False)/self.likelihood.scale()  # Mx1;  L^(-1).Kuu^(-1/2).Kuf.y

        # p = log N(0, Kfu.Kuu^(-1).Kuf + I/sigma^2) - 1/(2.sigma^2).Trace(Kff - Kfu.Kuu^(-1).Kuf)
        p = -self.log_marginal_likelihood_constant
        p -= L.diagonal().log().sum() # 0.5 is taken as the square root of L
        p -= 0.5*self.X.shape[0]*self.likelihood.scale().log()
        p -= 0.5*y.T.mm(y).squeeze()/self.likelihood.scale()
        p += 0.5*c.T.mm(c).squeeze()
        p -= 0.5*(Kff_diag.sum() - Q.trace())/self.likelihood.scale() # trace
        return p

    def log_marginal_likelihood(self):
        # maximize the lower bound
        return self.elbo()

    def predict(self, Xs, full=False, tensor=False, predict_y=True):
        with torch.no_grad():
            Xs = self._check_input(Xs)  # MxD
            if self.mean is not None:
                y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
            else:
                y = self.y  # Nx1

            Kus = self.kernel(self.Z(),Xs)  # MxS
            Kuf = self.kernel(self.Z(),self.X)  # MxN
            Kuu = self.kernel(self.Z())  # MxM

            Luu = self._cholesky(Kuu, add_jitter=True)  # MxM;  Kuu^(1/2)
            v = torch.linalg.solve_triangular(Luu,Kuf,upper=False)  # MxN;  Kuu^(-1/2).Kuf
            L = self._cholesky(v.mm(v.T)/self.likelihood.scale() + self.eye)  # MxM;  (Kuu^(-1/2).Kuf.Kfu.Kuu^(-1/2)/sigma^2 + I)^(1/2)

            a = torch.linalg.solve_triangular(Luu,Kus,upper=False)  # MxS;  Kuu^(-1/2).Kus
            b = torch.linalg.solve_triangular(L,a,upper=False)  # MxS;  L^(-1).Kuu^(-1/2).Kus
            c = torch.linalg.solve_triangular(L,v.mm(y),upper=False)/self.likelihood.scale()  # Mx1;  L^(-1).Kuu^(-1/2).Kuf.y

            # mu = sigma^(-2).Ksu.Kuu^(-1/2).(sigma^(-2).Kuu^(-1/2).Kuf.Kfu.Kuu^(-1/2) + I)^(-1).Kuu^(-1/2).Kuf.y
            mu = b.T.mm(c)  # Mx1
            if self.mean is not None:
                mu += self.mean(Xs).reshape(-1,1)  # Mx1

            # var = Kss - Qsf.(Qff + sigma^2 I)^(-1).Qfs
            # below is the equivalent but more stable version by using the matrix inversion lemma
            # var = Kss - Ksu.Kuu^(-1).Kus + Ksu.Kuu^(-1/2).(sigma^(-2).Kuu^(-1/2).Kuf.Kfu.Kuu^(-1/2) + I)^(-1).Kuu^(-1/2).Kus
            if full:
                Kss = self.kernel(Xs)  # MxM
                var = Kss - a.T.mm(a) + b.T.mm(b)  # MxM
                if predict_y:
                    eye = torch.eye(var.shape[0], device=config.device, dtype=config.dtype)
                    var += self.likelihood.scale() * eye
            else:
                Kss_diag = self.kernel.K_diag(Xs)  # M
                var = Kss_diag - a.T.square().sum(dim=1) + b.T.square().sum(dim=1)  # M
                if predict_y:
                    var += self.likelihood.scale()
                var = var.reshape(-1,1)

            if tensor:
                return mu, var
            else:
                return mu.cpu().numpy(), var.cpu().numpy()

class SparseHensman(Model):
    """
    A sparse Gaussian process regression based on Hensman et al. [1] with a non-Gaussian likelihood.

    Args:
        kernel (mogptk.gpr.kernel.Kernel): Kernel.
        X (torch.tensor): Input data of shape (data_points,input_dims).
        y (torch.tensor): Output data of shape (data_points,).
        Z (int,torch.tensor): Number of inducing points to be distributed over the input space. Passing a tensor of shape (inducing_points,input_dims) sets the initial positions of the inducing points.
        likelihood (mogptk.gpr.likelihood.Likelihood): Likelihood.
        jitter (float): Relative jitter of the diagonal's mean added to the kernel's diagonal before calculating the Cholesky.
        mean (mogptk.gpr.mean.Mean): Mean.
        name (str): Name of the model.

    [1] J. Hensman, et al., "Scalable Variational Gaussian Process Classification", 2015
    """
    # This version replaces mu_q by L.mu_q and sigma_q by L.sigma_q.L^T, where LL^T = Kuu
    # So that p(u) ~ N(0,1) and q(u) ~ N(L.mu_q, L.sigma_q.L^T)
    def __init__(self, kernel, X, y, Z=None, likelihood=GaussianLikelihood(variance=1.0),
                 jitter=1e-8, mean=None, name="SparseHensman"):
        super().__init__(kernel, X, y, likelihood, jitter, mean, name)

        n = self.X.shape[0]
        self.is_sparse = Z is not None
        if self.is_sparse:
            Z = init_inducing_points(Z, self.X, kernel)
            Z = self._check_input(Z)
            n = Z.shape[0]

        self.eye = torch.eye(n, device=config.device, dtype=config.dtype)
        self.log_marginal_likelihood_constant = 0.5*self.X.shape[0]*np.log(2.0*np.pi)
        self.q_mu = Parameter(torch.zeros(n,1), name="q_mu")
        self.q_sqrt = Parameter(torch.eye(n), name="q_sqrt")
        self.q_sqrt.num_parameters = int((n*n+n)/2)
        if self.is_sparse:
            self.Z = Parameter(Z, name="induction_points")
            if 1 < kernel.output_dims:
                self.Z.num_parameters -= self.Z().shape[0]
        else:
            self.Z = Parameter(self.X, trainable=False)  # don't use inducing points

        self._register_parameters(self.q_mu)
        self._register_parameters(self.q_sqrt)
        if self.is_sparse:
            self._register_parameters(self.Z)

    def kl_gaussian(self, q_mu, q_sqrt):
        S_diag = q_sqrt.diagonal().square() # NxN
        kl = -q_mu.shape[0]
        kl += q_mu.T.mm(q_mu).squeeze()  # Mahalanobis
        kl -= S_diag.log().sum()  # determinant of q_var
        kl += S_diag.sum()  # same as Trace(p_var^(-1).q_var)
        return 0.5*kl

    def elbo(self):
        if self.mean is not None:
            y = self.y - self.mean(self.X).reshape(-1,1)  # Nx1
        else:
            y = self.y  # Nx1

        if self.is_sparse:
            qf_mu, qf_var_diag = self._predict(self.X, full=False)
        else:
            Kff = self.kernel(self.X)
            Lff = self._cholesky(Kff, add_jitter=True)  # NxN

            qf_mu = Lff.mm(self.q_mu())
            if self.mean is not None:
                qf_mu -= self.mean(self.X).reshape(-1,1)  # Sx1

            qf_sqrt = Lff.mm(self.q_sqrt().tril())
            qf_var_diag = qf_sqrt.mm(qf_sqrt.T).diagonal().reshape(-1,1)

        var_exp = self.likelihood.variational_expectation(y, qf_mu, qf_var_diag, X=self.X)
        kl = self.kl_gaussian(self.q_mu(), self.q_sqrt())
        return var_exp - kl

    def log_marginal_likelihood(self):
        # maximize the lower bound
        return self.elbo()

    def _predict(self, Xs, full=False):
        Kuu = self.kernel(self.Z())
        Kus = self.kernel(self.Z(),Xs)  # NxS

        Luu = self._cholesky(Kuu, add_jitter=True)  # NxN
        a = torch.linalg.solve_triangular(Luu,Kus,upper=False)  # NxS;  Kuu^(-1/2).Kus
        b = self.q_sqrt().tril().T.mm(torch.linalg.solve_triangular(Luu,Kus,upper=False))

        mu = Kus.T.mm(torch.linalg.solve_triangular(Luu.T,self.q_mu(),upper=True))  # Sx1
        if full:
            Kss = self.kernel(Xs)  # SxS
            var = Kss - a.T.mm(a) + b.T.mm(b)  # SxS
        else:
            Kss_diag = self.kernel.K_diag(Xs)  # M
            var = Kss_diag - a.T.square().sum(dim=1) + b.T.square().sum(dim=1)  # M
            var = var.reshape(-1,1)
        return mu, var

    def predict(self, Xs, full=False, tensor=False, predict_y=True):
        with torch.no_grad():
            Xs = self._check_input(Xs)  # MxD

            mu, var = self._predict(Xs, full=full)
            if predict_y:
                mu, var = self.likelihood.predict(mu, var, full=full, X=Xs)
            if self.mean is not None:
                mu += self.mean(Xs).reshape(-1,1)  # Sx1

            if tensor:
                return mu, var
            else:
                return mu.cpu().numpy(), var.cpu().numpy()

class Hensman(SparseHensman):
    """
    A Gaussian process regression based on Hensman et al. [1] with a non-Gaussian likelihood. This is equivalent to the `SparseHensman` model if we set the inducing points equal to the data points and by turning off training the inducing points.

    Args:
        kernel (mogptk.gpr.kernel.Kernel): Kernel.
        X (torch.tensor): Input data of shape (data_points,input_dims).
        y (torch.tensor): Output data of shape (data_points,).
        likelihood (mogptk.gpr.likelihood.Likelihood): Likelihood.
        jitter (float): Relative jitter of the diagonal's mean added to the kernel's diagonal before calculating the Cholesky.
        mean (mogptk.gpr.mean.Mean): Mean.
        name (str): Name of the model.

    [1] J. Hensman, et al., "Scalable Variational Gaussian Process Classification", 2015
    """
    def __init__(self, kernel, X, y, likelihood=GaussianLikelihood(variance=1.0), jitter=1e-8,
                 mean=None, name="Hensman"):
        super().__init__(kernel, X, y, None, likelihood, jitter, mean, name)
