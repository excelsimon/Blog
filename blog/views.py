#coding:utf-8

import os
import sys
import logging
import json

from django.shortcuts import render_to_response, render, get_object_or_404, HttpResponseRedirect, Http404
from django.views.generic.list import ListView
from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView, FormView
from django.contrib import auth
from django.template.context import RequestContext
from django.contrib.auth.decorators import login_required, permission_required
#from django.utils.decorators import method_decorator
from django.contrib.auth.models import User, Group
from django.core.urlresolvers import reverse
from django.core.cache import cache
from django.db.models import Q
from django.http import HttpResponse, FileResponse
from django.utils import timezone
# 缓存推荐在urls那里添加
from django.views.decorators.cache import cache_page #缓存  

import qrcode
from cStringIO import StringIO
import markdown2
from haystack.views import SearchView
from rest_framework import viewsets

from blog.serializers import UserSerializer, GroupSerializer
from blog.models import Article, Category, Tag, BlogComment, UserProfile, VisitorIP
from blog.tasks import save_client_ip
from .forms import RegistForm, UserForm, RetrieveForm, SearchForm, BlogCommentForm

BASEPATH = sys.path[0]
UPLOADPATH = os.path.join(BASEPATH, 'blog/media/uploads')
LENGTH_IN_RIGHT_INDEX = 14

def add_views_or_likes(target_article, views_or_likes):
    # A likes add two weight, a view add one weight
    if views_or_likes == 'views':
        target_article.views += 1
        target_article.weight += 1
    else:
        target_article.likes += 1
        target_article.weight += 2
    return True

def get_context_data_all(**kwargs):
    kwargs['category_list'] = Category.objects.all()
    kwargs['date_archive'] = Article.objects.archive()
    kwargs['tag_list'] = Tag.objects.all()
    visitor_ip = VisitorIP.objects.all()[:5]
    for visitor in visitor_ip:
        ip_split = visitor.ip.split('.')
        visitor.ip = '%s.*.*.%s' % (ip_split[0], ip_split[3])
    kwargs['visitor_ip'] = visitor_ip
    kwargs['visitor_num'] = cache.get('visitor_num')
    recent_comment = BlogComment.objects.order_by('-created_time')[:5]
    for comment in recent_comment:
        if len(comment.body) > LENGTH_IN_RIGHT_INDEX:
            comment.body = comment.body[:LENGTH_IN_RIGHT_INDEX + 1] + '...'
    kwargs['recent_comment'] = recent_comment
    hot_article = Article.objects.filter(status='p').order_by('-weight','-created_time')[:5]
    for article in hot_article:
        if len(article.title) > LENGTH_IN_RIGHT_INDEX:
            article.title = article.title[:LENGTH_IN_RIGHT_INDEX + 1] + '...' 
    kwargs['hot_article'] = hot_article
    return kwargs

def get_client_ip(request):
    if 'HTTP_X_FORWARDED_FOR' in request.META:
        return request.META['HTTP_X_FORWARDED_FOR']
    else:
        return request.META['REMOTE_ADDR']

class CachePageMixin(object):
    @classmethod
    def as_view(cls, **initkargs):
        view = super(CachePageMixin, cls).as_view(**initkargs)
        return cache_page(60 * 120)(view)

class LoginRequiredMixin(object):
    @classmethod
    def as_view(cls, **initkwargs):
        view = super(LoginRequiredMixin, cls).as_view(**initkwargs)
        return login_required(view)
    
#LoginRequiredMixin放最左边,多重继承时有先后顺序，从右开始，广度优先    
class AccountView(LoginRequiredMixin, ListView):
    template_name = "blog/index.html"
    context_object_name = "article_list"

    def get_queryset(self):
        # 一篇文章多个评论，只能过滤出一个文章, distinct无参数,如果需要过滤具体的不重复参数（如不重复的title）,可以.values('title').distince()
        article_list = Article.objects.filter(comment__commentator__username=self.request.user.username, status='p').distinct()
        return article_list

    
    def get_context_data(self, **kwargs):
        kwargs = get_context_data_all()
        return super(AccountView, self).get_context_data(**kwargs)
    

class IndexView(ListView):
    template_name = "blog/index.html"
    context_object_name = "article_list"

    def get_queryset(self):
        # models中已经定义了meta类，所以可以不用.order_by('name')
        article_list = Article.objects.filter(created_time__lte=timezone.now(), status='p')
        client_ip= get_client_ip(self.request)
        save_client_ip.delay(client_ip)
        # cache.set('tcdlejl', 'value', timeout=100)
        # logging.info(cache.get('tcdlejl'))
        return article_list

    def get_context_data(self, **kwargs):
        kwargs = get_context_data_all()
        return super(IndexView, self).get_context_data(**kwargs)


class ArticleDetailView(DetailView):
    model = Article
    template_name = "blog/detail.html"
    context_object_name = "article"
    pk_url_kwarg = 'article_id'

    def get_object(self, queryset=None):
        obj = super(ArticleDetailView, self).get_object()
        # 未发表文章不能显示
        if obj.status == 'd':
            raise Http404
        add_views_or_likes(target_article=obj, views_or_likes='views')
        obj.save()
        obj.body = markdown2.markdown(obj.body,['codehilite'], extras=['fenced-code-blocks'])
        obj.attachment_url = obj.attachment_url.split('/')
        return obj

    
    def get_context_data(self, **kwargs):
        kwargs['comment_list'] = self.object.comment.all()
        kwargs['comment_nums'] = self.object.comment.count()
        kwargs['form'] = BlogCommentForm()
        return super(ArticleDetailView, self).get_context_data(**kwargs)

@login_required
def upload(request, article_id):
    article_url = reverse('blog:detail', args=(article_id,))
    if request.method == 'GET':
        return HttpResponseRedirect(article_url)
    else:
        target_article = get_object_or_404(Article, pk=article_id)
        if target_article.status == 'd':
            return HttpResponseRedirect('/')
        myfile = request.FILES.get('uploadfile', None)
        if not myfile :
            return HttpResponse('No upload files!')
        myfilename = myfile.name
        rightformat = len(myfilename.split('/')) < 2
        if not rightformat:
            return HttpResponse('File name error!')
        elif myfilename in target_article.attachment_url:
            return HttpResponse('File exists!')
        folderpath = os.path.join(UPLOADPATH, '%s/' % article_id)
        try:
            os.mkdir(folderpath)
        except OSError, e:
            logging.error(e)
        filepath = os.path.join(folderpath, myfilename)
        with open(filepath, 'wb') as f:
            if myfile.multiple_chunks():
                f.write(myfile.read())
            else:
                for chunk in myfile.chunks():
                    f.write(chunk)
        target_article.attachment_url += '%s/' % myfilename
        target_article.save()
        return HttpResponse('Upload success!')

# arguments can be:login_url, raise_exception
# or can use permission.py, @perm_check
# @permission_required('blog.download_file', raise_exception=True)
from permission import check_blog_permission
@check_blog_permission
def download(request, param1, param2):
    article_id = param1
    file_id = int(param2)
    article_url = reverse('blog:detail', args=(article_id,))
    if request.method == 'POST':
        return HttpResponseRedirect(article_url)
    else:
        target_article = get_object_or_404(Article, pk=article_id)
        if target_article.status == 'd':
            return HttpResponseRedirect('/')

        def file_iterator(file_name, chunk_size=512):
            with open(file_name, 'rb') as f:
                while True:
                    c = f.read(chunk_size)
                    if c:
                        yield c
                    else:
                        break

        try:
            file_name = target_article.attachment_url.split('/')[file_id - 1]
        except IndexError:
            logging.error('No such file!')
        file_path = os.path.join(UPLOADPATH, article_id, file_name)
        response = FileResponse(file_iterator(file_path))
        response['Content-Type'] = 'application/octet-stream'
        response['Content-Disposition'] = 'attachment;filename=%s' % file_name.encode('utf-8')
        return response

class CategoryView(ListView):
    template_name = "blog/index.html"
    context_object_name = "article_list"

    def get_queryset(self):
        article_list = Article.objects.filter(category=self.kwargs['cate_id'], status='p')
        return article_list

    def get_context_data(self, **kwargs):
        kwargs = get_context_data_all()
        return super(CategoryView, self).get_context_data(**kwargs)


class TagView(ListView):
    template_name = "blog/index.html"
    context_object_name = "article_list"

    def get_queryset(self):
        article_list = Article.objects.filter(tags=self.kwargs['tag_id'], status='p')
        return article_list

    def get_context_data(self, **kwargs):
        kwargs = get_context_data_all()
        return super(TagView, self).get_context_data(**kwargs)


class ArchiveView(ListView):
    template_name = "blog/index.html"
    context_object_name = "article_list"

    def get_queryset(self):
        year = int(self.kwargs['year'])
        month = int(self.kwargs['month'])
        article_list = Article.objects.filter(status='p', created_time__year=year, created_time__month=month)
        return article_list

    def get_context_data(self, **kwargs):
        kwargs = get_context_data_all()
        return super(ArchiveView, self).get_context_data(**kwargs)

    
class CommentPostView(LoginRequiredMixin, FormView):
    form_class = BlogCommentForm
    template_name = 'blog/detail.html'


    def form_valid(self, form):
        target_article = get_object_or_404(Article, pk=self.kwargs['article_id'])
        if target_article.status == 'd':
            return HttpResponseRedirect('/')
        body = form.cleaned_data['body']
        BlogComment.objects.create(commentator=self.request.user, body=body, article=target_article)
        '''comment = form.save(commit=False)
        comment.commentator = self.request.user
        comment.article = target_article
        comment.save()'''
        self.success_url = reverse('blog:detail', args=(target_article.id,))
        return HttpResponseRedirect(self.success_url)

    def form_invalid(self, form):
        target_article = get_object_or_404(Article, pk=self.kwargs['article_id'])
        if target_article.status == 'd':
            return HttpResponseRedirect('/')
        return render(self.request, 'blog/detail.html', {
            'form': form,
            'article': target_article,
            'comment_list': target_article.comment.all(),
            'comment_nums': target_article.comment.count()
        })

class LoginView(FormView):
    form_class = UserForm
    template_name = 'blog/login.html'
    
    def form_valid(self, form):
        username = form.cleaned_data['username']
        password = form.cleaned_data['password']
        user = auth.authenticate(username=username, password=password)
        # print user
        if user and user.is_active:
            auth.login(self.request, user)
            # 利用session传递信息给模板层
            self.request.session['username'] = username
            self.request.session['userimg'] = user.userprofile.userimg
            return HttpResponseRedirect('/')
        else:
            login_info = "Username or password is error"
            return render(self.request, 'blog/login.html', {'form': form,'login_info':login_info})
    
    def form_invalid(self, form):
        login_info = 'input error'
        return render_to_response('blog/login.html', RequestContext(self.request, {'form': form, 'login_info':login_info}))
      
class MySearchView(SearchView, ListView):
    
    template_name = 'search/search.html'
    def extra_context(self):
        context = super(MySearchView, self).extra_context()
        side_list = Article.objects.filter(status='p')
        context['side_list'] = side_list
        return context
           
@login_required
def praise(request, article_id):
    # 前一个访问的页面，要去除/praise'
    # current_url = request.get_full_path()[:-7]
    method_error = -1
    status_error = -2
    user_error = -3
    result = {'error_code':0, 'likes':0}
    if request.method == 'POST':
        result['error_code'] = method_error
    else:
        target_article = get_object_or_404(Article, pk=article_id)
        if target_article.status == 'd':
            result['error_code'] = status_error
        elif str(request.user.id) in target_article.user_likes:
            result['error_code'] = user_error
        else:
            target_article.user_likes += '%s,' % request.user.id
            add_views_or_likes(target_article=target_article, views_or_likes='likes')
            target_article.save()
            result['likes'] = target_article.likes
    return HttpResponse(json.dumps(result))

def search(request):

    if request.method == 'POST':
        form = SearchForm(request.POST)
        if form.is_valid():
            body_search = form.cleaned_data['body_search']
            article_list = Article.objects.filter(Q(title__icontains=body_search) | Q(body__icontains=body_search)).distinct()
            return render(request, 'blog/index.html', {'article_list':article_list})
        else:
            return HttpResponseRedirect('/')
    else:
        form = SearchForm(request.GET)
        if form.is_valid():
            body_search = form.cleaned_data['body_search']
            #article_list = Article.objects.filter(Q(title__icontains=body_search) | Q(body__icontains=body_search)).distinct()
            article_list = form.search()
            return render(request, 'blog/index.html', {'article_list':article_list})
        else:
            return HttpResponseRedirect('/')

            
def regist(request):
    regist_info = ''
    if request.method == 'GET':
        form = RegistForm()
        # context, content, contents are the same(变量名不影响使用)
        contents = {'form':form}
        
        # thise are the same
        return render(request, 'blog/regist.html', contents)
    else:
        form = RegistForm(request.POST, request.FILES)
        if form.is_valid():
            username = form.cleaned_data['username']
            password1 = form.cleaned_data['password1']
            password2 = form.cleaned_data['password2']
            nickname = form.cleaned_data['nickname']
            phone     = form.cleaned_data['phone']
            userimg = form.cleaned_data['userimg']
            if password1 == password2:
                user_filter_result = User.objects.filter(username=username) 
                nickname_filter_result = UserProfile.objects.filter(nickname=nickname)
                if user_filter_result or nickname_filter_result:  
                    regist_info = "邮箱或昵称已存在"
                    return render_to_response("blog/regist.html", RequestContext(request,{'form':form, 'regist_info':regist_info}))  
                else:
                    user = User.objects.create_user(username=username,password=password1)
                    # user.is_active=True  
                    # user.save
                    user_profile = UserProfile()
                    user_profile.user_id = user.id
                    user_profile.phone = phone
                    user_profile.nickname = nickname
                    user_profile.userimg = '/media/uploads/userimg/defaultuser.png'
                    if userimg:
                        imgpath = os.path.join(UPLOADPATH, 'userimg', username)
                        with open(imgpath, 'wb') as img:
                            img.write(userimg.read())
                        user_profile.userimg = imgpath[(len(BASEPATH) + 5):]
                    user_profile.save()
                    regist_info = '注册成功'
                    user = auth.authenticate(username=username, password=password1)
                    auth.login(request, user)
                    # 利用session传递信息给模板层
                    request.session['username'] = username
                    request.session['userimg'] = user.userprofile.userimg
                    return HttpResponseRedirect('/')
                    return render_to_response('blog/regist.html', RequestContext(request, {'form': form,'regist_info':regist_info}))
            else:
                regist_info = "两次输入的密码不一致!" 
                return render_to_response("blog/regist.html", RequestContext(request,{'form':form, 'regist_info':regist_info}))  
        else:
            regist_info = '输入有误'
            return render_to_response('blog/regist.html', RequestContext(request, {'form': form, 'regist_info':regist_info}))
        
@login_required
def logout(request):
    auth.logout(request)
    return HttpResponseRedirect("/")            
 
def retrieve(request):
    retrieve_info = ''
    user_not_exist = False
    if request.method == 'GET':
        form = RetrieveForm()
        return render_to_response('blog/retrieve.html', RequestContext(request, {'form':form}))
    else:
        form = RetrieveForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data['username']
            phone     = form.cleaned_data['phone']
            password1 = form.cleaned_data['password1']
            password2 = form.cleaned_data['password2']
            try:
                user = get_object_or_404(User, username=username)
            except:
                user_not_exist = True
            if  user_not_exist or not user.is_active:  
                retrieve_info = "用户名不存在"
                # 这里在template中可以直接调用form或者retrieve_info
                return render_to_response("blog/retrieve.html", RequestContext(request,{'form':form, 'retrieve_info':retrieve_info}))  
            else:
                try:
                    user_profile = get_object_or_404(UserProfile, user_id=user.id)
                except Http404:
                    retrieve_info = "该用户不可修改密码"
                    return render_to_response("blog/retrieve.html", RequestContext(request,{'form':form, 'retrieve_info':retrieve_info}))  
                    
                phone_db = user_profile.phone
                if phone_db == phone:
                    if password1 == password2:
                        user.set_password(password1)
                        user.save()
                        retrieve_info = '密码修改成功'
                    else:
                        retrieve_info = "两次输入的密码不一致!" 
                else:
                    retrieve_info = "手机号有误"
                return render_to_response('blog/retrieve.html', RequestContext(request, {'form': form,'retrieve_info':retrieve_info}))
        else:
            retrieve_info = 'input error'
            return render_to_response('blog/retrieve.html', RequestContext(request, {'form': form, 'retrieve_info':retrieve_info}))
    
            
def generate_qrcode(request):

    if request.method == 'POST':
        try:
            url_data = request.POST['target_url']
        except:
            return HttpResponseRedirect('/')            
            
        if url_data == '':
            img = qrcode.make(request.get_host())
        else:
            img = qrcode.make(url_data)
        buf = StringIO()
        img.save(buf)
        image_stream = buf.getvalue()
        import base64
        image_stream = base64.b64encode(image_stream)

        response = HttpResponse(image_stream, content_type='image/png')
        return response
    else:
        return HttpResponseRedirect('/')            

     
# For API
class UserViewSet(viewsets.ModelViewSet):
    '''
    API enpoint that allows users to be viewed or edited.
    '''
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer

class GroupViewSet(viewsets.ModelViewSet):
    '''
    API endpoint that allows groups to be viewed or edited.
    '''
    queryset = Group.objects.all()
    serializer_class = GroupSerializer

