#Sizing_Loop.py
#Created:  Jun 2016, M. Vegh
#Modified: Feb 2017, M. Vegh

# ----------------------------------------------------------------------
#  Imports
# ----------------------------------------------------------------------

from SUAVE.Core import Data
from SUAVE.Surrogate.svr_surrogate_functions import check_svr_accuracy
import scipy.interpolate as interpolate

import sklearn.svm as svm
import sklearn.ensemble as ensemble
import sklearn.gaussian_process as gaussian_process
import sklearn.linear_model as linear_model
from sklearn.pipeline import make_pipeline
import sklearn.neighbors as neighbors
from sklearn.preprocessing import PolynomialFeatures
from write_sizing_outputs import write_sizing_outputs
from read_sizing_inputs import read_sizing_inputs
from read_sizing_residuals import read_sizing_residuals
from write_sizing_residuals import write_sizing_residuals
import numpy as np
import scipy as sp
import copy
import time
try:
    import pyOpt
except: 
    ImportError
    
class Sizing_Loop_Residuals(Data):
    def __defaults__(self):
        #parameters common to all methods
        self.tolerance                = 1E-4
        self.initial_step             = None  #'Default', 'Table', 'SVR', GPR, Neighbors
        self.guess_from_residuals     = False
        self.sizing_logic             = [0] #list based on sizing residuals to determine aircraft families
        self.sizing_logic_list        = [[0]] #list of sizing logic results that can be seen
        self.family_sizing            = False
        self.residual_objective_index = 0 #which index to be minimized in sub optimization
        self.residual_optimizer       = pyOpt.pySNOPT.SNOPT() #'SciPy_SLSQP
        self.residual_filename        = 'sizing_residuals.txt'
        self.update_method            = None  #'successive_substitution', 'newton-raphson', ;broyden
        self.default_y                = None  #default inputs in case the guess is very far from 
        self.default_scaling          = None  #scaling value to make sizing parameters ~1
        self.maximum_iterations       = None  #cutoff point for sizing loop to close
        self.output_filename          = None
        self.sizing_evaluation        = None  #defined in the Procedure script
        self.max_y                    = None
        self.min_y                    = None
        self.hard_max_bound           = False
        self.hard_min_bound           = True
        self.backtracking             = True
        self.write_threshhold         = 3     #number of iterations before it writes, regardless of how close it is to currently written values
        self.max_error                = 0.    #maximum error
       
        #parameters that may only apply to certain methods
        self.iteration_options                                   = Data()
        self.iteration_options.newton_raphson_tolerance          = 5E-2             #threshhold of convergence when you start using newton raphson
        self.iteration_options.max_newton_raphson_tolerance      = 2E-3             #threshhold at which newton raphson is no longer used (to prevent overshoot and extra iterations)
        self.iteration_options.h                                 = 1E-6             #finite difference step for Newton iteration
        self.iteration_options.initialize_jacobian               = 'newton-raphson' #how Jacobian is initialized for broyden; newton-raphson by default
        self.iteration_options.jacobian                          = np.array([np.nan])
        self.iteration_options.write_jacobian                    = True
        self.iteration_options.max_initial_step                  = 1.               #maximum distance at which interpolation is allowed
        self.iteration_options.min_fix_point_iterations          = 2                #minimum number of iterations to perform fixed-point iteration before starting newton-raphson
        self.iteration_options.min_surrogate_step                = .011             #minimum distance at which SVR is used (if closer, table lookup is used)
        self.iteration_options.min_write_step                    = .011             #minimum distance at which sizing data are written
        self.iteration_options.min_surrogate_length              = 4                #minimum number data points needed before SVR is used
        self.iteration_options.number_of_surrogate_calls         = 0
        
        self.iteration_options.minimum_training_samples         = 1E6
        self.iteration_options.newton_raphson_damping_threshhold = 5E-5
        
        
        backtracking                         = Data()
        backtracking.backtracking_flag       = True     #True means you do backtracking when err isn't decreased
        backtracking.threshhold              = 1.      # factor times the msq at which to terminate backtracking
        backtracking.max_steps               = 5
        backtracking.multiplier               = .5
        
        #assign
        self.iteration_options.backtracking  = backtracking

    def evaluate(self, nexus):
        
        if nexus.optimization_problem != None: #make it so you can run sizing without an optimization problem
            unscaled_inputs = nexus.optimization_problem.inputs[:,1] #use optimization problem inputs here
            input_scaling   = nexus.optimization_problem.inputs[:,3]
            scaled_inputs   = unscaled_inputs/input_scaling
            
            
            problem_inputs = []
            for value in scaled_inputs:
                problem_inputs.append(value)  #writing to file is easier when you use list
            
            nexus.problem_inputs = problem_inputs
            #opt_flag = 1 #tells if you're running an optimization case or not-used in writing outputs
        #else:
            #opt_flag = 0
  
        opt_flag = 1
        #unpack inputs
        tol               = self.tolerance #percentage difference in mass and energy between iterations
        h                 = self.iteration_options.h 
        y                 = self.default_y
        max_iter          = self.maximum_iterations
        scaling           = self.default_scaling
        sizing_evaluation = self.sizing_evaluation
        iteration_options = self.iteration_options
        err               = [1000] #initialize error
        
        #initialize
        converged         = 0     #marker to tell if it's converged
        j=0  #major iterations
        i=0  #function evals
        
        
        #determine the initial step
        min_norm = 1000.
        sizing_logic = self.sizing_logic
        if self.initial_step != 'Default':
            if self.family_sizing:
                min_dist          = []
                data_inputs_list  = []
                data_outputs_list = []
                read_success_list = []
                min_norm_list     = []
                i_min_norm_list   = []
                
                for j in xrange(len(self.sizing_logic_list)):
                    data_inputs, data_outputs, read_success = read_sizing_inputs(self, self.output_filename+str(sizing_logic)+'.txt', scaled_inputs)
                    read_success_list.append(read_success)
                    data_inputs_list.append(data_inputs)
                    data_outputs_list.append(data_outputs)
                    if read_success:
                        min_norm, imin_norm = find_min_norm(scaled_inputs, data_inputs)
                        min_norm_list.append(min_norm)
                        i_min_norm_list.append(imin_norm)
                    else:
                        min_norm_list.append(1E9)
                        i_min_norm_list.append(0)
                    
                min_norm     = np.min(min_norm_list)
                i_logic      = np.argmin(min_norm_list) #index relating to closest point 
                data_inputs  = data_inputs_list[i_logic]
                data_outputs = data_outputs_list[i_logic]
                read_success = read_success_list[i_logic]
            else:       
                data_inputs, data_outputs, read_success = read_sizing_inputs(self, self.output_filename+'.txt', scaled_inputs)
                if read_success:
                    min_norm, i_min_dist = find_min_norm(scaled_inputs, data_inputs)
                
            sizing_data, residual_data, read_success_residuals = read_sizing_residuals(self, scaled_inputs)
                
            if read_success: 
               
                
                if min_norm<iteration_options.max_initial_step: #make sure data is close to current guess
                    if self.initial_step == 'Table' or min_norm<iteration_options.min_surrogate_step or len(data_outputs[:,0])< iteration_options.min_surrogate_length:
                        regr    = neighbors.KNeighborsRegressor( n_neighbors = 1)
        
                    else:
                        print 'running surrogate method'
                        if self.initial_step == 'SVR':
                            #for SVR, can optimize parameters C and eps for closest point
                            print 'optimizing svr parameters'
                            x = [2.,-1.] #initial guess for 10**C, 10**eps
                        
                            t1=time.time()
                            out = sp.optimize.minimize(check_svr_accuracy, x, method='Nelder-Mead', args=(data_inputs, data_outputs, imin_dist))
                            t2=time.time()
                            c_out = 10**out.x[0]
                            eps_out = 10**out.x[1]
                            if c_out > 1E10:
                                c_out = 1E10
                            if eps_out<1E-8:
                                eps_out = 1E-8
                 
             
                            
                            regr        = svm.SVR(C=c_out,  epsilon = eps_out)
                            
                        elif self.initial_step == 'GradientBoosting':
                            regr        = ensemble.GradientBoostingRegressor()
                            
                        elif self.initial_step == 'ExtraTrees':
                            regr        = ensemble.ExtraTreesRegressor()
                        
                        elif self.initial_step == 'RandomForest':
                            regr        = ensemble.RandomForestRegressor()
                        
                        elif self.initial_step == 'Bagging':
                            regr        = ensemble.BaggingRegressor()
                            
                        elif self.initial_step == 'GPR':
                            regr        = gaussian_process.GaussianProcess()
                        elif self.initial_step == 'PF2_GPR':
                            regr        = make_pipeline(PolynomialFeatures(degree = 2), gaussian_process.GaussianProcess())
                        
                        elif self.initial_step == 'RANSAC':
                            regr        = linear_model.RANSACRegressor()
                        
                        elif self.initial_step == 'Neighbors':
                            n_neighbors = min(iteration_options.n_neighbors, len(data_outputs))
                            if iteration_options.neighbors_weighted_distance  == True:
                                regr    = neighbors.KNeighborsRegressor( n_neighbors = n_neighbors ,weights = 'distance')
                            
                            else:  
                                regr    = neighbors.KNeighborsRegressor( n_neighbors = n_neighbors)
                        
                        #now run the fits/guesses  
                    
                        iteration_options.number_of_surrogate_calls += 1
                    y = []  
                    main_regr = regr
                    input_for_regr = scaled_inputs.reshape(1,-1)
                    
                    
                    
                   
                    if self.guess_from_residuals and read_success_residuals and  len(data_outputs[:,0])> iteration_options.min_surrogate_length:  
                        #run a constrained optimization problem on this
                       
                        resids_surrogates = []
                        for j in xrange(len(residual_data[0,:])):
                            regr            = copy.copy(main_regr)
                            resid_surrogate = regr.fit(sizing_data, residual_data[:,j])
                            resids_surrogates.append(resid_surrogate)
                            
                        residual_problem                        = Residual_Problem()
                        residual_problem.objective_index        = self.residual_objective_index
                        residual_problem.constraints_surrogates = resids_surrogates
                        residual_problem.x                      = scaled_inputs
                        opt_prob = pyOpt.Optimization('residual problem', residual_problem.compute)
                        for j in xrange(len(self.default_y)): #add design variables
                            opt_prob.addVar('x'+str(j+1), 'c', lower = self.min_y[j], upper = self.max_y[j], value = self.default_y[j])
                        for j in xrange(len(resids_surrogates)):
                            opt_prob.addCon('g'+str(j+1), type = 'e', equal = self.tolerance)
                        opt_prob.addObj('f')
                        opt_sizing_outputs = self.residual_optimizer(opt_prob, sens_step = 1E-2)
                        print 'opt_sizing_outputs =', opt_sizing_outputs
                        print 'opt_sizing_outputs[0] = ', opt_sizing_outputs[0] 
                        print 'opt_sizing_outputs[1] = ', opt_sizing_outputs[1]
                        
                        y = opt_sizing_outputs[1]
                        
                        print 'y = ', y
                        #now residuals surrogates created, now set up an optimization problem
                        #start with SLSQP as optimizer
                        
                    else:
                        for j in xrange(len(data_outputs[0,:])):
                            y_surrogate = regr.fit(data_inputs, data_outputs[:,j])
                            y.append(y_surrogate.predict(input_for_regr)[0])    
                            #check if it goes outside bounds for sizing variables: use table in this case
                            #y_check, bound_violated = self.check_bounds(y)
                            
                            if y[j] > self.max_y[j] or y[j]< self.min_y[j]:
                                print 'sizing variable range violated, val = ', y[j], ' j = ', j
                                n_neighbors = min(iteration_options.n_neighbors, len(data_outputs))
                                regr_backup = neighbors.KNeighborsRegressor( n_neighbors = n_neighbors)
                                y_surrogate = regr_backup.fit(data_inputs, data_outputs[:,j])
                                y[j]        = y_surrogate.predict(input_for_regr)[0]
                            
                        y = np.array(y)
                    

        # initialize previous sizing values
        y_save   = 2*y  #save values to detect oscillation
        y_save2  = 3*y
        norm_dy2 = 1   #used to determine if it's oscillating; if so, do a successive_substitution iteration
  
        #handle input data
        
        nr_start = 0 #flag to switch between methods; if you do nr too early, sizing diverges
        
        #now start running the sizing loop
        while np.max(np.abs(err))>tol:   
            iteration_options.err_save = err
            if self.update_method == 'successive_substitution':
                err,y, i   = self.successive_substitution_update(y,err, sizing_evaluation, nexus, scaling, i, iteration_options)
               
            elif self.update_method == 'damped_successive_substitution':
                if i == 0:
                    err,y, i   = self.successive_substitution_update(y,err, sizing_evaluation, nexus, scaling, i, iteration_options)
                else:
                    err,y, i   = self.successive_substitution_update((y+y_save)/2.,err, sizing_evaluation, nexus, scaling, i, iteration_options)
                    
            elif self.update_method == 'newton-raphson' or self.update_method =='damped_newton':
                if i==0:
                    nr_start=0  
                if np.max(np.abs(err))> self.iteration_options.newton_raphson_tolerance or np.max(np.abs(err))<self.iteration_options.max_newton_raphson_tolerance or i<self.iteration_options.min_fix_point_iterations:
                    err,y, i = self.successive_substitution_update(y,err, sizing_evaluation, nexus, scaling, i, iteration_options)
                

                else:          
                    if nr_start==0:
                        if self.update_method == 'newton-raphson':
                            err,y, i   = self.newton_raphson_update(y_save2, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                        elif self.update_method == 'damped_newton':
                            err,y, i   = self.damped_newton_update(y_save2, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                        nr_start   = 1
                    else:
                        if self.update_method == 'newton-raphson':
                            err,y, i   = self.newton_raphson_update(y, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                        elif self.update_method == 'damped_newton':
                            err,y, i   = self.damped_newton_update(y, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                        nr_start   = 1
            
            elif self.update_method == 'broyden':
                
                if (np.max(np.abs(err))> self.iteration_options.newton_raphson_tolerance or np.max(np.abs(err))<self.iteration_options.max_newton_raphson_tolerance or i<self.iteration_options.min_fix_point_iterations) and nr_start ==0:
                    if i>1:  #obtain this value so you can get an ok value initialization from the Jacobian w/o finite differincing
                        err_save   = iteration_options.err_save
                    err,y, i   = self.successive_substitution_update(y,err, sizing_evaluation, nexus, scaling, i, iteration_options)
                    nr_start   = 0 #in case broyden update diverges
                    
                else:
                    
                    if nr_start==0:
                        if self.iteration_options.initialize_jacobian == 'newton-raphson':
                            err,y, i   = self.newton_raphson_update(y_save2, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                        
                        
                        else:
                            #from http://www.jnmas.org/jnmas2-5.pdf
                            D = np.diag((y-y_save2)/(err-self.iteration_options.err_save))
                            self.iteration_options.y_save = y_save
                            self.iteration_options.Jinv = D
                        
                            err,y, i   = self.broyden_update(y, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                     
                        nr_start = 1
                        
                    else:
                        err,y, i   = self.broyden_update(y, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                      
            y  = self.stay_inbounds(y_save, y)           
            dy  = y-y_save
            dy2 = y-y_save2
            norm_dy  = np.linalg.norm(dy)
            norm_dy2 = np.linalg.norm(dy2)

            
            print 'norm(dy) = ', norm_dy
            print 'norm(dy2) = ', norm_dy2
            #handle stuck oscillatory behavior
            if self.iteration_options.backtracking.backtracking_flag == True:
                err_save           = iteration_options.err_save
                backtracking       = iteration_options.backtracking
                back_thresh        = backtracking.threshhold
                i_back             = 0
                min_err_back       = 1000.
                y_back_list        = [y]
                err_back_list      = [err]
                norm_err_back_list = [np.linalg.norm(err)]
                
                while np.linalg.norm(err)>back_thresh*np.linalg.norm(err_save) and i_back<backtracking.max_steps  : #while?
                    print 'backtracking'
                    print 'err, err_save = ', np.linalg.norm(err), np.linalg.norm(err_save)
                    p = y-y_save
                    
                    backtrack_y = y_save+p*(backtracking.multiplier**(i_back+1))
                    
                    
                    print 'y, y_save, backtrack_y = ', y, y_save, backtrack_y
                    err,y_back, i     = self.successive_substitution_update(backtrack_y, err, sizing_evaluation, nexus, scaling, i, iteration_options)
                    
                    y_back_list.append(backtrack_y)
                    err_back_list.append(err)
                    norm_err_back_list.append(np.linalg.norm(err))
                    min_err_back = min(np.linalg.norm(err_back_list), min_err_back)
                    i_back+=1
                
                i_min_back = np.argmin(norm_err_back_list)
                y          = y_back_list[i_min_back]
                err        = err_back_list[i_min_back]
                if len(norm_err_back_list)>1:
                    print 'norm_err_back_list = ', norm_err_back_list, ' i_min_back = ', i_min_back
                    print 'norm_err_save = ', np.linalg.norm(err_save)
           
    
        
            
            #keep track of previous iterations, as they're used to transition between methods + for saving results
            y_save2 = 1.*y_save
            y_save = 1. *y  
            print 'y_save2 = ', y_save2
            print 'y_save = ', y_save
            
            print 'err = ', err
            
            #now figure out how close things are
            if read_success_residuals:
                #print 'y_save = ', y_save
                #print 'scaled_inputs = ', scaled_inputs
                current_point = np.concatenate((y_save, scaled_inputs))
                #print ' current_point = ',  current_point
                #print 'residual_data = ', residual_data
                min_norm, imin_dist = find_min_norm(sizing_data, current_point)
               
            if min_norm>2. or read_success_residuals==0:
                write_sizing_residuals(self, y_save, scaled_inputs, err)
            
            
            #uncomment this when you want to write error at each iteration
            
            file=open('y_err_values.txt', 'ab')   
            file.write('global iteration = ')
            file.write(str( nexus.total_number_of_iterations))
            
            
            file.write(', iteration = ')
            file.write(str(i))
            file.write(', x = ')
            file.write(str(scaled_inputs))
            file.write(', y = ')
            file.write(', ')
            file.write(str(y_save2))
            file.write(', err = ')
            file.write(str(err.tolist()))
            file.write('\n') 
            file.close()
            
            j+=1
            
            if i>max_iter: #
                #err=float('nan')*np.ones(np.size(err))
                print "###########sizing loop did not converge##########"
                break
    
        if i<max_iter and not np.isnan(err).any() and opt_flag == 1:  #write converged values to file
            converged = 1
            #check how close inputs are to what we already have        
            if converged and (min_norm>self.iteration_options.min_write_step or i>self.write_threshhold): #now output to file, writing when it's either not a FD step, or it takes a long time to converge
            #make sure they're in right format      
            #use y_save2, as it makes derivatives consistent
                if self.family_sizing:
                    write_sizing_outputs(self.output_filename+str(sizing_logic)+'.txt', y_save2, problem_inputs)
                else:
                    write_sizing_outputs(self.output_filename+'.txt', y_save2, problem_inputs)
        else:
            converged = 0
        nexus.sizing_loop.converged = converged

        nexus.total_number_of_iterations += i
        nexus.number_of_iterations = i #function calls
        
        #nexus.mass_guess=mass
        results=nexus.results
        
    
        print 'number of function calls=', i
        print 'number of iterations total=', nexus.total_number_of_iterations

        nexus.sizing_loop.max_error   = max(err)
        nexus.sizing_loop.output_error = err
        nexus.distance_to_closest_point = min_norm
        nexus.sizing_variables = y_save2
    
        
        return nexus
        
    def successive_substitution_update(self,y, err, sizing_evaluation, nexus, scaling, iter, iteration_options):
        err_out, y_out = sizing_evaluation(y, nexus, scaling)
        iter += 1
        
        
        #iteration_options.err_save = err
        #y_out, bound_violated = self.check_bounds(y_out)
        #make sure it's in bounds
        y_out = self.stay_inbounds(y, y_out)
        return err_out, y_out, iter
    
    def newton_raphson_update(self,y, err, sizing_evaluation, nexus, scaling, iter, iteration_options):
        h = iteration_options.h
        print '###begin Finite Differencing###'
        J, iter = Finite_Difference_Gradient(y,err, sizing_evaluation, nexus, scaling, iter, h)
        print '###end Finite Differencing###'
        try:
    
            Jinv =np.linalg.inv(J)  
            p = -np.dot(Jinv,err)
            y_update = y + p
      
     
            print 'y_update here = ', y_update
            y_update = self.stay_inbounds(y, y_update)
            
            err_out, y_out = sizing_evaluation(y_update, nexus, scaling)
            iter += 1

            print 'p before = ', p
            p = y_update-y #back this out in case of bounds
            print 'p after = ', p
            '''
            if np.linalg.norm(err_out)>np.linalg.norm(err):
                print 'backtracking'
                y_update = y+p/2.
                err_out, y_out = sizing_evaluation(y_update, nexus, scaling)  
                iter += 1 
            '''
            
            #save these values in case of Broyden update
            iteration_options.Jinv     = Jinv
            iteration_options.jacobian = J
            iteration_options.y_save   = y
            #iteration_options.err_save = err
            
            #write results for Jacobian at every iteration
            unscaled_inputs = nexus.optimization_problem.inputs[:,1] #use optimization problem inputs here
            input_scaling   = nexus.optimization_problem.inputs[:,3]
            scaled_inputs   = unscaled_inputs/input_scaling

            file=open('Jacobian_values.txt', 'ab')   
            file.write('global iteration = ')
            file.write(str( nexus.total_number_of_iterations))
            
            
            file.write(', iteration = ')
            file.write(str(iter))
            file.write(', x = ')
            file.write(str(scaled_inputs))
            file.write(', err = ')
            file.write(str(err_out))
            file.write(', eigenvalues = ')
            file.write(str(np.linalg.eig(J)[0]))
            file.write(', condition number = ')
            file.write(str(np.linalg.cond(J )))
            #file.write(', Jacobian = ')
            #file.write(str(J))
           
            file.write('\n') 
            file.close()
            
            
            
            print 'err_out=', err_out
            
        except np.linalg.LinAlgError:
            print 'singular Jacobian detected, use successive_substitution'
            err_out, y_update, iter = self.successive_substitution_update(y, err, sizing_evaluation, nexus, scaling, iter, iteration_options)
        
       
        return err_out, y_update, iter
        
    def broyden_update(self,y, err, sizing_evaluation, nexus, scaling, iter, iteration_options):
        y_save      = iteration_options.y_save
        err_save    = iteration_options.err_save 
        dy          = y - y_save
        df          = err - err_save
        Jinv        = iteration_options.Jinv
        print 'Jinv=', Jinv
        update_step = ((dy - Jinv*df)/np.linalg.norm(df))* df
        print 'update_step=', update_step
        Jinv_out    = Jinv + update_step
        
        p                      = -np.dot(Jinv_out,err)
        y_update               = y + p
        
        err_out, y_out         = sizing_evaluation(y_update, nexus, scaling)
        
        
        #pack outputs
        iteration_options.Jinv     = Jinv_out
        iteration_options.jacobian = np.linalg.inv(Jinv)
        #iteration_options.err_save = err  #save previous iteration
        iteration_options.y_save   = y
        iter                       = iter+1
        
        return err_out, y_update, iter
        
    def damped_newton_update(self,y, err, sizing_evaluation, nexus, scaling, iter, iteration_options):
        #uses newton raphson, does backtracking linesearch if it goes too far
        tol = self.tolerance
        h = iteration_options.h
        print '###begin Finite Differencing###'
        J, iter = Finite_Difference_Gradient(y,err, sizing_evaluation, nexus, scaling, iter, h)
        try:  
            Jinv =np.linalg.inv(J)  
            p = -np.dot(Jinv,err)
            y_update = y + p
            
            

            err_out, y_out = sizing_evaluation(y_update, nexus, scaling)
            iter += 1 
            norm_error =np.linalg.norm(err_out)
            
            if norm_error<self.iteration_options.newton_raphson_damping_threshhold:
                    old_norm = np.linalg.norm(err)
                    ydamp = y+.5*p #halve the step
                    err_out, y_out = sizing_evaluation(ydamp, nexus, scaling)
                    y_update = ydamp
            #save these values in case of Broyden update
            iteration_options.Jinv     = Jinv 
            iteration_options.y_save   = y
            iteration_options.err_save = err
            
            print 'err_out=', err_out
                
        except np.linalg.LinAlgError:
            print 'singular Jacobian detected, use successive_substitution'
            err_out, y_update, iter = self.successive_substitution_update(y, err, sizing_evaluation, nexus, scaling, iter, iteration_options)
        
        
        return err_out, y_update, iter
       
    def check_bounds(self, y):
        y_out = 1.*y #create copy
        bound_violated = 0
        for j in xrange(len(y)):  #handle variable bounds to prevent going to weird areas (such as negative mass)
            if self.hard_min_bound:
                if y[j]<self.min_y[j]:
                    y_out[j] = self.min_y[j]*1.
                    bound_violated = 1
            if self.hard_max_bound:
                if y[j]>self.max_y[j]:
                    y_out[j] = self.max_y[j]*1.
                    bound_violated = 1
        return y_out, bound_violated
    
    def stay_inbounds(self, y, y_update):
        
        sizing_evaluation     = self.sizing_evaluation
        scaling               = self.default_scaling
        p                     = y_update-y #search step

        y_out, bound_violated = self.check_bounds(y_update)

        backtrack_step        = self.iteration_options.backtracking.multiplier
        bounds_violated       = 1 #counter to determine how many bounds are violated
        while bound_violated:
            print 'bound violated, backtracking'
            print 'y_update, y_out = ',  y_update, y_out
            bound_violated = 0
            for j in xrange(len(y_out)):
                if not np.isclose(y_out[j], y_update[j]) or np.isnan(y_update).any():
                    y_update = y+p*backtrack_step
           
                    
                    bounds_violated = bounds_violated+1
                    backtrack_step = backtrack_step*.5
                    break

            y_out, bound_violated = self.check_bounds(y_update)
        return y_update
    
    
        
    __call__ = evaluate
    
class Residual_Problem(Data):
    def __defaults__(self):
        self.sizing_loop               = None
        self.objective_index           = 0
        self.constraints_surrogates    = None
        self.x                         = None
    def compute(self, y):  #x is fixed in this problem, change y
        x = self.x
        f = y[self.objective_index]
        input_vals = [y.tolist()+x.tolist()]
        print 'x = ', x
        print 'input_vals = ', input_vals
        
        g = []
        for j in range(len(self.constraints_surrogates)):
            g.append(self.constraints_surrogates[j].predict(input_vals)[0])
        #g = np.array(g) #uncomment if particular surrogate saves each value as array
        
        fail  = np.array(np.isnan(f.tolist()) or np.isnan(np.array(g).any())).astype(int)
        #print 'f,g = ',  f, g
        print 'g = ', g
        return f, g, fail
        
    __call__ = compute


    
def Finite_Difference_Gradient(x,f , my_function, inputs, scaling, iter, h):
    #use forward difference

    J=np.nan*np.ones([len(x), len(x)])
    for i in xrange(len(x)):
        xu=1.*x;
        xu[i]=x[i]+h *x[i]  #use FD step of H*x
        fu, y_out = my_function(xu, inputs,scaling)
        
        print 'fbase=', f
        J[:,i] = (fu-f)/(xu[i]-x[i])
        iter=iter+1
        


    return J, iter

def find_min_norm(scaled_inputs, data_inputs):
    min_norm = 1E9
    diff = np.subtract(scaled_inputs, data_inputs) #check how close inputs are to tabulated values  
    #find minimum entry and corresponding index 
    imin_dist = -1 
    for k in xrange(len(diff[:,-1])):
        row = diff[k,:]
        row_norm = np.linalg.norm(row)
        if row_norm < min_norm:
            min_norm = row_norm
            imin_dist = k*1 
    
    return min_norm, imin_dist
    